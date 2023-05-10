# Copyright 2023 Kakao Brain and The HuggingFace Team. All rights reserved.
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

import math
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np
import torch

from ..configuration_utils import ConfigMixin, register_to_config
from ..utils import BaseOutput, randn_tensor
from .scheduling_utils import SchedulerMixin

def dynamic_threholding_test(x):
    x2 = torch.clone(x).cpu().detach().numpy()
    p = 99.5
    s = np.percentile(np.abs(x2), p, axis=tuple(range(1, x2.ndim)))[0]
    s = max(s, 1.0)
    x = torch.clip(x, -s, s) / s
    return x  # x.clamp(-1, 1)


@dataclass
# Copied from diffusers.schedulers.scheduling_ddpm.DDPMSchedulerOutput with DDPM->UnCLIP
class UnCLIPSchedulerOutput(BaseOutput):
    """
    Output class for the scheduler's step function output.

    Args:
        prev_sample (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)` for images):
            Computed sample (x_{t-1}) of previous timestep. `prev_sample` should be used as next model input in the
            denoising loop.
        pred_original_sample (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)` for images):
            The predicted denoised sample (x_{0}) based on the model output from the current timestep.
            `pred_original_sample` can be used to preview progress or for guidance.
    """

    prev_sample: torch.FloatTensor
    pred_original_sample: Optional[torch.FloatTensor] = None


# Copied from diffusers.schedulers.scheduling_ddpm.betas_for_alpha_bar
def betas_for_alpha_bar(num_diffusion_timesteps, max_beta=0.999):
    """
    Create a beta schedule that discretizes the given alpha_t_bar function, which defines the cumulative product of
    (1-beta) over time from t = [0,1].

    Contains a function alpha_bar that takes an argument t and transforms it to the cumulative product of (1-beta) up
    to that part of the diffusion process.


    Args:
        num_diffusion_timesteps (`int`): the number of betas to produce.
        max_beta (`float`): the maximum beta to use; use values lower than 1 to
                     prevent singularities.

    Returns:
        betas (`np.ndarray`): the betas used by the scheduler to step the model outputs
    """

    def alpha_bar(time_step):
        return math.cos((time_step + 0.008) / 1.008 * math.pi / 2) ** 2

    betas = []
    for i in range(num_diffusion_timesteps):
        t1 = i / num_diffusion_timesteps
        t2 = (i + 1) / num_diffusion_timesteps
        betas.append(min(1 - alpha_bar(t2) / alpha_bar(t1), max_beta))
    return torch.tensor(betas, dtype=torch.float32)


class UnCLIPScheduler(SchedulerMixin, ConfigMixin):
    """
    This is a modified DDPM Scheduler specifically for the karlo unCLIP model.

    This scheduler has some minor variations in how it calculates the learned range variance and dynamically
    re-calculates betas based off the timesteps it is skipping.

    The scheduler also uses a slightly different step ratio when computing timesteps to use for inference.

    See [`~DDPMScheduler`] for more information on DDPM scheduling

    Args:
        num_train_timesteps (`int`): number of diffusion steps used to train the model.
        variance_type (`str`):
            options to clip the variance used when adding noise to the denoised sample. Choose from `fixed_small_log`
            or `learned_range`.
        clip_sample (`bool`, default `True`):
            option to clip predicted sample between `-clip_sample_range` and `clip_sample_range` for numerical
            stability.
        clip_sample_range (`float`, default `1.0`):
            The range to clip the sample between. See `clip_sample`.
        prediction_type (`str`, default `epsilon`, optional):
            prediction type of the scheduler function, one of `epsilon` (predicting the noise of the diffusion process)
            or `sample` (directly predicting the noisy sample`)
        thresholding (`bool`, default `False`):
            whether to use the "dynamic thresholding" method (introduced by Imagen, https://arxiv.org/abs/2205.11487).
            Note that the thresholding method is unsuitable for latent-space diffusion models (such as
            stable-diffusion).
        dynamic_thresholding_ratio (`float`, default `0.995`):
            the ratio for the dynamic thresholding method. Default is `0.995`, the same as Imagen
            (https://arxiv.org/abs/2205.11487). Valid only when `thresholding=True`.
        sample_max_value (`float`, default `1.0`):
            the threshold value for dynamic thresholding. Valid only when `thresholding=True`.
    """

    @register_to_config
    def __init__(
        self,
        num_train_timesteps: int = 1000,
        variance_type: str = "fixed_small_log",
        clip_sample: bool = True,
        clip_sample_range: Optional[float] = 1.0,
        thresholding: bool = False,
        dynamic_thresholding_ratio: float = 0.995,
        sample_max_value: float = 1.0,
        prediction_type: str = "epsilon",
        beta_schedule: str = "squaredcos_cap_v2", # "linear"
        beta_start: float = 0.0001,
        beta_end: float = 0.02,

    ):

        if beta_schedule == "squaredcos_cap_v2":
            self.betas = betas_for_alpha_bar(num_train_timesteps)
        elif beta_schedule == "linear":
            # Linear schedule from Ho et al, extended to work for any number of diffusion steps.
            scale = 1000 / num_train_timesteps
            self.betas = torch.linspace(beta_start * scale, beta_end * scale, num_train_timesteps, dtype=torch.float64)
        else:
            raise NotImplementedError(f"{beta_schedule} does is not implemented for {self.__class__}")


        self.alphas = 1.0 - self.betas
        self.alphas_cumprod = torch.cumprod(self.alphas, dim=0)
        self.one = torch.tensor(1.0)

        # standard deviation of the initial noise distribution
        self.init_noise_sigma = 1.0

        # setable values
        self.num_inference_steps = None
        self.timesteps = torch.from_numpy(np.arange(0, num_train_timesteps)[::-1].copy())

        self.variance_type = variance_type

    def scale_model_input(self, sample: torch.FloatTensor, timestep: Optional[int] = None) -> torch.FloatTensor:
        """
        Ensures interchangeability with schedulers that need to scale the denoising model input depending on the
        current timestep.

        Args:
            sample (`torch.FloatTensor`): input sample
            timestep (`int`, optional): current timestep

        Returns:
            `torch.FloatTensor`: scaled input sample
        """
        return sample

    def set_timesteps(self, num_inference_steps: int, device: Union[str, torch.device] = None):
        """
        Sets the discrete timesteps used for the diffusion chain. Supporting function to be run before inference.

        Note that this scheduler uses a slightly different step ratio than the other diffusers schedulers. The
        different step ratio is to mimic the original karlo implementation and does not affect the quality or accuracy
        of the results.

        Args:
            num_inference_steps (`int`):
                the number of diffusion steps used when generating samples with a pre-trained model.
        """
        self.num_inference_steps = num_inference_steps
        step_ratio = (self.config.num_train_timesteps - 1) / (self.num_inference_steps - 1)
        timesteps = (np.arange(0, num_inference_steps) * step_ratio).round()[::-1].copy().astype(np.int64)
        self.timesteps = torch.from_numpy(timesteps).to(device)

    def _get_variance(self, t, prev_timestep=None, predicted_variance=None, variance_type=None):
        if prev_timestep is None:
            prev_timestep = t - 1

        alpha_prod_t = self.alphas_cumprod[t]
        alpha_prod_t_prev = self.alphas_cumprod[prev_timestep] if prev_timestep >= 0 else self.one
        beta_prod_t = 1 - alpha_prod_t
        beta_prod_t_prev = 1 - alpha_prod_t_prev

        if prev_timestep == t - 1:
            beta = self.betas[t]
        else:
            beta = 1 - alpha_prod_t / alpha_prod_t_prev

        # For t > 0, compute predicted variance βt (see formula (6) and (7) from https://arxiv.org/pdf/2006.11239.pdf)
        # and sample from it to get previous sample
        # x_{t-1} ~ N(pred_prev_sample, variance) == add variance to pred_sample
        variance = beta_prod_t_prev / beta_prod_t * beta

        if variance_type is None:
            variance_type = self.config.variance_type

        # hacks - were probably added for training stability
        if variance_type == "fixed_small_log":
            variance = torch.log(torch.clamp(variance, min=1e-20))
            variance = torch.exp(0.5 * variance)
        elif variance_type == "learned_range":
            # NOTE difference with DDPM scheduler
            min_log = variance.log()
            max_log = beta.log()

            frac = (predicted_variance + 1) / 2
            # this is log variance
            variance = frac * max_log + (1 - frac) * min_log


        return variance

    def _threshold_sample(self, sample: torch.FloatTensor) -> torch.FloatTensor:
        """
        "Dynamic thresholding: At each sampling step we set s to a certain percentile absolute pixel value in xt0 (the
        prediction of x_0 at timestep t), and if s > 1, then we threshold xt0 to the range [-s, s] and then divide by
        s. Dynamic thresholding pushes saturated pixels (those near -1 and 1) inwards, thereby actively preventing
        pixels from saturation at each step. We find that dynamic thresholding results in significantly better
        photorealism as well as better image-text alignment, especially when using very large guidance weights."

        https://arxiv.org/abs/2205.11487
        """
        dtype = sample.dtype
        batch_size, channels, height, width = sample.shape

        if dtype not in (torch.float32, torch.float64):
            sample = sample.float()  # upcast for quantile calculation, and clamp not implemented for cpu half

        # Flatten sample for doing quantile calculation along each image
        sample = sample.reshape(batch_size, channels * height * width)

        abs_sample = sample.abs()  # "a certain percentile absolute pixel value"

        s = torch.quantile(abs_sample, self.config.dynamic_thresholding_ratio, dim=1)
        s = torch.clamp(
            s, min=1, max=self.config.sample_max_value
        )  # When clamped to min=1, equivalent to standard clipping to [-1, 1]

        s = s.unsqueeze(1)  # (batch_size, 1) because clamp will broadcast along dim=0
        sample = torch.clamp(sample, -s, s) / s  # "we threshold xt0 to the range [-s, s] and then divide by s"

        sample = sample.reshape(batch_size, channels, height, width)
        sample = sample.to(dtype)

        return sample

    def step(
        self,
        model_output: torch.FloatTensor,
        timestep: int,
        sample: torch.FloatTensor,
        prev_timestep: Optional[int] = None,
        generator=None,
        return_dict: bool = True,
        batch_size: Optional[int] = None,
    ) -> Union[UnCLIPSchedulerOutput, Tuple]:
        """
        Predict the sample at the previous timestep by reversing the SDE. Core function to propagate the diffusion
        process from the learned model outputs (most often the predicted noise).

        Args:
            model_output (`torch.FloatTensor`): direct output from learned diffusion model.
            timestep (`int`): current discrete timestep in the diffusion chain.
            sample (`torch.FloatTensor`):
                current instance of sample being created by diffusion process.
            prev_timestep (`int`, *optional*): The previous timestep to predict the previous sample at.
                Used to dynamically compute beta. If not given, `t-1` is used and the pre-computed beta is used.
            generator: random number generator.
            return_dict (`bool`): option for returning tuple rather than UnCLIPSchedulerOutput class

        Returns:
            [`~schedulers.scheduling_utils.UnCLIPSchedulerOutput`] or `tuple`:
            [`~schedulers.scheduling_utils.UnCLIPSchedulerOutput`] if `return_dict` is True, otherwise a `tuple`. When
            returning a tuple, the first element is the sample tensor.

        """
        t = timestep

        if model_output.shape[1] == sample.shape[1] * 2 and self.variance_type == "learned_range":
            model_output, predicted_variance = torch.split(model_output, sample.shape[1], dim=1)
        else:
            predicted_variance = None

        # 1. compute alphas, betas
        if prev_timestep is None:
            prev_timestep = t - 1

        alpha_prod_t = self.alphas_cumprod[t]
        alpha_prod_t_prev = self.alphas_cumprod[prev_timestep] if prev_timestep >= 0 else self.one
        beta_prod_t = 1 - alpha_prod_t
        beta_prod_t_prev = 1 - alpha_prod_t_prev

        if prev_timestep == t - 1:
            beta = self.betas[t]
            alpha = self.alphas[t]
        else:
            beta = 1 - alpha_prod_t / alpha_prod_t_prev
            alpha = 1 - beta

        # 2. compute predicted original sample from predicted noise also called
        # "predicted x_0" of formula (15) from https://arxiv.org/pdf/2006.11239.pdf
        if self.config.prediction_type == "epsilon":
            pred_original_sample = (sample - beta_prod_t ** (0.5) * model_output) / alpha_prod_t ** (0.5)
        elif self.config.prediction_type == "sample":
            pred_original_sample = model_output
        else:
            raise ValueError(
                f"prediction_type given as {self.config.prediction_type} must be one of `epsilon` or `sample`"
                " for the UnCLIPScheduler."
            )

        # 3. Clip/threhold "predicted x_0" 
        if self.config.clip_sample:
            pred_original_sample = torch.clamp(
                pred_original_sample, -self.config.clip_sample_range, self.config.clip_sample_range
            )

        if self.config.thresholding:
            # yiyi Notes, testing with dynamic_threholding_test, need to make it work with _threshold_sample
            #pred_original_sample = self._threshold_sample(pred_original_sample)
            pred_original_sample = dynamic_threholding_test(pred_original_sample)
            
        # 4. Compute coefficients for pred_original_sample x_0 and current sample x_t
        # See formula (7) from https://arxiv.org/pdf/2006.11239.pdf
        pred_original_sample_coeff = (alpha_prod_t_prev ** (0.5) * beta) / beta_prod_t
        current_sample_coeff = alpha ** (0.5) * beta_prod_t_prev / beta_prod_t

        # 5. Compute predicted previous sample µ_t
        # See formula (7) from https://arxiv.org/pdf/2006.11239.pdf
        pred_prev_sample = pred_original_sample_coeff * pred_original_sample + current_sample_coeff * sample

        # 6. Add noise
        variance = 0
        if t > 0:
            if batch_size is not None:
                assert batch_size * 2 == model_output.shape[0]
                variance_noise = randn_tensor(
                    (batch_size, *model_output.shape[1:]) , dtype=model_output.dtype, generator=generator, device=model_output.device
                )     

                variance_noise = torch.cat([variance_noise, variance_noise], dim=0)
            else:
                variance_noise = randn_tensor(model_output.shape , dtype=model_output.dtype, generator=generator, device=model_output.device)  

            variance = self._get_variance(
                t,
                predicted_variance=predicted_variance,
                prev_timestep=prev_timestep,
            )

            if self.variance_type == "fixed_small_log":
                variance = variance
            elif self.variance_type == "learned_range":
                variance = (0.5 * variance).exp()
            else:
                raise ValueError(
                    f"variance_type given as {self.variance_type} must be one of `fixed_small_log` or `learned_range`"
                    " for the UnCLIPScheduler."
                )

            variance = variance * variance_noise

        pred_prev_sample = pred_prev_sample + variance

        if not return_dict:
            return (pred_prev_sample,)

        return UnCLIPSchedulerOutput(prev_sample=pred_prev_sample, pred_original_sample=pred_original_sample)
