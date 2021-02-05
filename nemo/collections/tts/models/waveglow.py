# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
from typing import Any, Dict, Optional

import torch
from hydra.utils import instantiate
from omegaconf import MISSING, DictConfig, OmegaConf, open_dict
from pytorch_lightning.loggers import LoggerCollection, TensorBoardLogger

from nemo.collections.tts.helpers.helpers import OperationMode, waveglow_log_to_tb_func
from nemo.collections.tts.losses.waveglowloss import WaveGlowLoss
from nemo.collections.tts.models.base import GlowVocoder
from nemo.core.classes import Exportable
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types.elements import (
    AudioSignal,
    LengthsType,
    LogDeterminantType,
    MelSpectrogramType,
    NormalDistributionSamplesType,
    VoidType,
)
from nemo.core.neural_types.neural_type import NeuralType
from nemo.utils import logging


@dataclass
class WaveglowConfig:
    waveglow: Dict[Any, Any] = MISSING
    preprocessor: Dict[Any, Any] = MISSING
    sigma: float = MISSING
    train_ds: Optional[Dict[Any, Any]] = None
    validation_ds: Optional[Dict[Any, Any]] = None


class WaveGlowModel(GlowVocoder, Exportable):
    """Waveglow model used to convert betweeen spectrograms and audio"""

    def __init__(self, cfg: DictConfig, trainer: 'Trainer' = None):
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)
        super().__init__(cfg=cfg, trainer=trainer)

        schema = OmegaConf.structured(WaveglowConfig)
        # ModelPT ensures that cfg is a DictConfig, but do this second check in case ModelPT changes
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)
        elif not isinstance(cfg, DictConfig):
            raise ValueError(f"cfg was type: {type(cfg)}. Expected either a dict or a DictConfig")
        # Ensure passed cfg is compliant with schema
        OmegaConf.merge(cfg, schema)

        self.sigma = self._cfg.sigma
        self.audio_to_melspec_precessor = instantiate(self._cfg.preprocessor)
        self.waveglow = instantiate(self._cfg.waveglow)
        self.loss = WaveGlowLoss()

    @GlowVocoder.mode.setter
    def mode(self, new_mode):
        if new_mode == OperationMode.training:
            self.train()
        else:
            self.eval()
        self._mode = new_mode
        self.waveglow.mode = new_mode

    @property
    def input_types(self):
        return {
            "audio": NeuralType(('B', 'T'), AudioSignal()),
            "audio_len": NeuralType(('B'), LengthsType()),
            "run_inverse": NeuralType(optional=True),
        }

    @property
    def output_types(self):
        if self.mode == OperationMode.training or self.mode == OperationMode.validation:
            output_dict = {
                "pred_normal_dist": NeuralType(('B', 'flowgroup', 'T'), NormalDistributionSamplesType()),
                "log_s_list": NeuralType(('B', 'flowgroup', 'T'), VoidType()),  # TODO: Figure out a good typing
                "log_det_W_list": NeuralType(elements_type=LogDeterminantType()),
            }
            if self.mode == OperationMode.validation:
                output_dict["audio_pred"] = NeuralType(('B', 'T'), AudioSignal())
                output_dict["spec"] = NeuralType(('B', 'T', 'D'), MelSpectrogramType())
                output_dict["spec_len"] = NeuralType(('B'), LengthsType())
            return output_dict
        return {
            "audio_pred": NeuralType(('B', 'T'), AudioSignal()),
        }

    @typecheck()
    def forward(self, *, audio, audio_len, run_inverse=True):
        if self.mode != self.waveglow.mode:
            raise ValueError(
                f"WaveGlowModel's mode {self.mode} does not match WaveGlowModule's mode {self.waveglow.mode}"
            )
        spec, spec_len = self.audio_to_melspec_precessor(audio, audio_len)
        tensors = self.waveglow(spec=spec, audio=audio, run_inverse=run_inverse, sigma=self.sigma)
        if self.mode == OperationMode.training:
            return tensors[:-1]  # z, log_s_list, log_det_W_list
        elif self.mode == OperationMode.validation:
            z, log_s_list, log_det_W_list, audio_pred = tensors
            return z, log_s_list, log_det_W_list, audio_pred, spec, spec_len
        return tensors  # audio_pred

    @typecheck(
        input_types={
            "spec": NeuralType(('B', 'D', 'T'), MelSpectrogramType()),
            "sigma": NeuralType(optional=True),
            "denoise": NeuralType(optional=True),
            "denoiser_strength": NeuralType(optional=True),
        },
        output_types={"audio": NeuralType(('B', 'T'), AudioSignal())},
    )
    def convert_spectrogram_to_audio(
        self, spec: torch.Tensor, sigma: float = 1.0, denoise: bool = True, denoiser_strength: float = 0.01
    ) -> torch.Tensor:
        with self.nemo_infer():
            self.waveglow.remove_weightnorm()
            audio = self.waveglow(
                spec=spec.to(self.waveglow.upsample.weight.dtype), run_inverse=True, audio=None, sigma=sigma
            )
            if denoise:
                audio = self.denoise(audio, denoiser_strength)

        return audio

    def training_step(self, batch, batch_idx):
        self.mode = OperationMode.training
        audio, audio_len = batch
        z, log_s_list, log_det_W_list = self(audio=audio, audio_len=audio_len, run_inverse=False)

        loss = self.loss(z=z, log_s_list=log_s_list, log_det_W_list=log_det_W_list, sigma=self.sigma)
        output = {
            'loss': loss,
            'progress_bar': {'training_loss': loss},
            'log': {'loss': loss},
        }
        return output

    def validation_step(self, batch, batch_idx):
        self.mode = OperationMode.validation
        audio, audio_len = batch
        z, log_s_list, log_det_W_list, audio_pred, spec, spec_len = self(
            audio=audio, audio_len=audio_len, run_inverse=(batch_idx == 0)
        )
        loss = self.loss(z=z, log_s_list=log_s_list, log_det_W_list=log_det_W_list, sigma=self.sigma)
        return {
            "val_loss": loss,
            "audio_pred": audio_pred,
            "mel_target": spec,
            "mel_len": spec_len,
        }

    def validation_epoch_end(self, outputs):
        if self.logger is not None and self.logger.experiment is not None:
            tb_logger = self.logger.experiment
            if isinstance(self.logger, LoggerCollection):
                for logger in self.logger:
                    if isinstance(logger, TensorBoardLogger):
                        tb_logger = logger.experiment
                        break
            waveglow_log_to_tb_func(
                tb_logger,
                outputs[0].values(),
                self.global_step,
                tag="eval",
                mel_fb=self.audio_to_melspec_precessor.fb,
            )
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        self.log('val_loss', avg_loss)

    def __setup_dataloader_from_config(self, cfg, shuffle_should_be: bool = True, name: str = "train"):
        if "dataset" not in cfg or not isinstance(cfg.dataset, DictConfig):
            raise ValueError(f"No dataset for {name}")
        if "dataloader_params" not in cfg or not isinstance(cfg.dataloader_params, DictConfig):
            raise ValueError(f"No dataloder_params for {name}")
        if shuffle_should_be:
            if 'shuffle' not in cfg.dataloader_params:
                logging.warning(
                    f"Shuffle should be set to True for {self}'s {name} dataloader but was not found in its "
                    "config. Manually setting to True"
                )
                with open_dict(cfg["dataloader_params"]):
                    cfg.dataloader_params.shuffle = True
            elif not cfg.dataloader_params.shuffle:
                logging.error(f"The {name} dataloader for {self} has shuffle set to False!!!")
        elif not shuffle_should_be and cfg.dataloader_params.shuffle:
            logging.error(f"The {name} dataloader for {self} has shuffle set to True!!!")

        dataset = instantiate(cfg.dataset)
        return torch.utils.data.DataLoader(dataset, collate_fn=dataset.collate_fn, **cfg.dataloader_params)

    def setup_training_data(self, cfg):
        self._train_dl = self.__setup_dataloader_from_config(cfg)

    def setup_validation_data(self, cfg):
        self._validation_dl = self.__setup_dataloader_from_config(cfg, shuffle_should_be=False, name="validation")

    @classmethod
    def list_available_models(cls) -> 'List[PretrainedModelInfo]':
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        list_of_models = []
        model = PretrainedModelInfo(
            pretrained_model_name="WaveGlow-22050Hz",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemottsmodels/versions/1.0.0a5/files/WaveGlow-22050Hz.nemo",
            description="This model is trained on LJSpeech sampled at 22050Hz, and can be used as an universal vocoder.",
            class_=cls,
        )
        list_of_models.append(model)
        return list_of_models

    def export(
        self,
        output: str,
        input_example=None,
        output_example=None,
        verbose=False,
        export_params=True,
        do_constant_folding=True,
        keep_initializers_as_inputs=False,
        onnx_opset_version: int = 12,
        try_script: bool = False,
        set_eval: bool = True,
        check_trace: bool = True,
        use_dynamic_axes: bool = True,
        check_tolerance=0.01,
    ):
        self.update_bias_spect()
        self.waveglow.export(
            output,
            input_example=input_example,
            output_example=output_example,
            verbose=verbose,
            export_params=export_params,
            do_constant_folding=do_constant_folding,
            keep_initializers_as_inputs=keep_initializers_as_inputs,
            onnx_opset_version=onnx_opset_version,
            try_script=try_script,
            set_eval=try_script,
            check_trace=check_trace,
            use_dynamic_axes=use_dynamic_axes,
            check_tolerance=check_tolerance,
        )
