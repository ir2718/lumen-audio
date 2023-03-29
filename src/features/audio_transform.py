from abc import ABC, abstractmethod

import librosa
import numpy as np
import torch
import torchvision.transforms.functional as F
from torchaudio.transforms import FrequencyMasking, TimeMasking
from torchvision.transforms import RandomErasing
from transformers import ASTFeatureExtractor, Wav2Vec2FeatureExtractor

import src.config.config_defaults as config_defaults
from src.utils.utils_audio import stereo_to_mono, time_stretch
from src.utils.utils_functions import EnumStr


class UnsupportedAudioTransforms(ValueError):
    pass


class AudioTransforms(EnumStr):
    """List of supported AudioTransforms we use."""

    AST = "ast"
    MEL_SPECTROGRAM_RESIZE_REPEAT = "mel_spectrogram_resize_repeat"
    MEL_SPECTROGRAM_FIXED_REPEAT = "mel_spectrogram_fixed_repeat"
    WAV2VEC = "wav2vec"


class SupportedAugmentations(EnumStr):
    """List of supported spectrogram augmentations we use."""

    TIME_STRETCH = "time_stretch"
    FREQ_MASK = "freq_mask"
    TIME_MASK = "time_mask"
    RANDOM_ERASE = "random_erase"
    RANDOM_PIXELS = "radnom_pixels"


class AudioTransformBase(ABC):
    """Base class for all audio transforms. Ideally, each audio transform class should be self
    contained and shouldn't depened on the outside context.

    Audio transfrom can be model dependent. We can create audio transforms which work only for one
    model and that's fine.
    """

    def __init__(
        self,
        sampling_rate: int,
        augmentation_enums: list[SupportedAugmentations] = [
            SupportedAugmentations.TIME_STRETCH,
            SupportedAugmentations.FREQ_MASK,
            SupportedAugmentations.TIME_MASK,
            SupportedAugmentations.RANDOM_ERASE,
            SupportedAugmentations.RANDOM_PIXELS,
        ],
        stretch_factors=[0.8, 1.2],
        freq_mask_param=30,
        time_mask_param=30,
        hide_random_pixels_p=0.25,
        std_noise=0.01,
    ) -> None:
        super().__init__()
        self.sampling_rate = sampling_rate
        self.augmentation_enums = augmentation_enums
        self.stretch_factors = stretch_factors
        self.freq_mask_param = freq_mask_param
        self.time_mask_param = time_mask_param
        self.has_augmentations = len(augmentation_enums) > 0
        self.hide_random_pixels_p = hide_random_pixels_p
        self.std_noise = std_noise

    @abstractmethod
    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor]:
        """Function which prepares everything for model's .forward() function. It creates the
        spectrogram from audio.

        Args:
            audio: audio data
            original_sr: _description_

        Returns:
            tuple[torch.Tensor, torch.Tensor]: _description_
        """


class AudioTransformAST(AudioTransformBase):

    """Resamples audio, converts it to mono, does AST feature extraction which extracts spectrogram
    (mel filter banks) from audio.

    Warning: resampling should be done here. AST does the job.
    """

    def __init__(
        self,
        pretrained_tag=config_defaults.DEFAULT_AST_PRETRAINED_TAG,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.feature_extractor = ASTFeatureExtractor.from_pretrained(pretrained_tag)

    def apply_augmentations(self, spectrogram: torch.Tensor):
        if SupportedAugmentations.FREQ_MASK in self.augmentation_enums:
            spectrogram = FrequencyMasking(freq_mask_param=self.freq_mask_param)(
                spectrogram
            )
        if SupportedAugmentations.TIME_MASK in self.augmentation_enums:
            spectrogram = TimeMasking(time_mask_param=self.time_mask_param)(spectrogram)

        if SupportedAugmentations.RANDOM_ERASE in self.augmentation_enums:
            spectrogram = RandomErasing(p=1)(spectrogram)

        if SupportedAugmentations.RANDOM_PIXELS in self.augmentation_enums:
            mask = (
                torch.FloatTensor(*spectrogram.shape).uniform_()
                < self.hide_random_pixels_p
            )
            num_of_masked_elements = mask.sum()
            noise = torch.normal(
                mean=spectrogram.mean(),
                std=self.std_noise,
                size=(num_of_masked_elements,),
            )

            spectrogram[mask] = noise
        return spectrogram

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        audio = stereo_to_mono(audio)

        if SupportedAugmentations.TIME_STRETCH in self.augmentation_enums:
            l, r = self.stretch_factors
            stretch_rate = np.random.uniform(l, r)
            # size_before = len(audio)
            audio = librosa.effects.time_stretch(y=audio, rate=stretch_rate)
            # audio = audio[:size_before]

        features = self.feature_extractor(
            audio,
            sampling_rate=self.sampling_rate,
            return_tensors="pt",
        )

        # mel filter banks
        spectrogram = features["input_values"]
        assert (
            len(spectrogram.shape) == 3
        ), "Spectrogram has to have 3 dimensions before torch augmentations!"
        spectrogram = self.apply_augmentations(spectrogram)
        spectrogram = spectrogram.squeeze(dim=0)

        return spectrogram


class MelSpectrogramOurs(AudioTransformBase):
    """Resamples audio and extracts melspectrogram from audio."""

    def __init__(
        self,
        n_fft: int = config_defaults.DEFAULT_N_FFT,
        hop_length: int = config_defaults.DEFAULT_HOP_LENGTH,
        n_mels: int = config_defaults.DEFAULT_N_MELS,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> torch.Tensor:
        audio = stereo_to_mono(audio)
        audio_resampled = librosa.resample(
            audio,
            orig_sr=original_sr,
            target_sr=self.sampling_rate,
        )

        spectrogram = librosa.feature.melspectrogram(
            y=audio_resampled,
            sr=self.sampling_rate,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            n_mels=self.n_mels,
        )

        return spectrogram


class MelSpectrogramResize(MelSpectrogramOurs):
    """Resamples audio, extracts melspectrogram from audio, resizes it to the given dimensions."""

    def __init__(self, dim: tuple[int, int], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dim = dim

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:

        spectrogram = super().process(audio, original_sr)

        spectrogram = spectrogram.reshape(1, 1, *spectrogram.shape)
        spectrogram = F.resize(torch.tensor(spectrogram), size=self.dim, antialias=True)
        spectrogram = spectrogram.reshape(1, *self.dim)

        return spectrogram


class MelSpectrogramFixed(MelSpectrogramOurs):
    """Resamples audio, extracts melspectrogram from audio and pads the original spectrogram to
    dimension of spectrogram for max_len sequence."""

    def __init__(self, max_len: int, dim: tuple[int, int], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_len = max_len
        self.dim = dim

        FAKE_SAMPLE_RATE = 44_100
        dummy_audio = np.random.random(size=(max_len * FAKE_SAMPLE_RATE,))
        audio_resampled = librosa.resample(
            dummy_audio,
            orig_sr=FAKE_SAMPLE_RATE,
            target_sr=self.sampling_rate,
        )

        spectrogram = librosa.feature.melspectrogram(
            y=audio_resampled,
            sr=self.sampling_rate,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            n_mels=self.n_mels,
        )

        self.seq_dim = spectrogram.shape

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor]:

        spectrogram = super().process(audio, original_sr)

        spectrogram_padded = np.zeros(self.seq_dim)
        w, h = spectrogram.shape
        spectrogram_padded[:w, :h] = spectrogram
        spectrogram_padded = spectrogram_padded.reshape(1, 1, *self.seq_dim)
        spectrogram_padded = F.resize(
            torch.tensor(spectrogram_padded), size=self.dim, antialias=True
        )
        spectrogram_padded = spectrogram_padded.reshape(1, *self.dim)

        return spectrogram_padded.type(torch.float32)


class MelSpectrogramFixedRepeated(MelSpectrogramFixed):
    """Calls MelSpectrogramFixed and repeats the output 3 times.

    This is useful for mocking RGB channels.
    """

    def __init__(self, repeat=3, **kwargs):
        super().__init__(**kwargs)
        self.repeat = repeat

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:

        spectrogram = super().process(audio, original_sr)
        spectrogram = spectrogram.repeat(1, self.repeat, 1, 1)[0]

        return spectrogram


class MelSpectrogramResizedRepeated(MelSpectrogramFixed):
    """Calls MelSpectrogramResize and repeats the output 3 times.

    This is useful for mocking RGB channels.
    """

    def __init__(self, repeat=3, **kwargs):
        super().__init__(**kwargs)
        self.repeat = repeat

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:

        spectrogram = super().process(audio, original_sr)
        spectrogram = spectrogram.repeat(1, self.repeat, 1, 1)[0]

        return spectrogram


class AudioTransformWav2Vec2(AudioTransformBase):
    def __init__(
        self,
        pretrained_tag=config_defaults.DEFAULT_WAV2VEC_PRETRAINED_TAG,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(
            config_defaults.DEFAULT_WAV2VEC_PRETRAINED_TAG
        )

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
        original_sr: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        audio = stereo_to_mono(audio)

        if SupportedAugmentations.TIME_STRETCH in self.augmentation_enums:
            min_s, max_s = self.stretch_factors
            audio = time_stretch(audio, min_s, max_s, trim=True)

        audio = librosa.util.fix_length(audio, size=original_sr * 3)

        features_dict = self.feature_extractor(
            audio,
            sampling_rate=self.sampling_rate,
            return_tensors="pt",
            padding=True,
        )
        features = features_dict.input_values.squeeze(0)

        return features


def get_audio_transform(
    audio_transform_enum: AudioTransforms,
    sampling_rate: int,
    augmentation_enums: list[SupportedAugmentations],
    dim: tuple[int, int],
    **aug_kwargs,
) -> AudioTransformBase:

    if audio_transform_enum is AudioTransforms.AST:
        return AudioTransformAST(
            sampling_rate=sampling_rate,
            pretrained_tag=config_defaults.DEFAULT_AST_PRETRAINED_TAG,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    elif audio_transform_enum is AudioTransforms.MEL_SPECTROGRAM_FIXED_REPEAT:
        return MelSpectrogramFixedRepeated(
            sampling_rate=sampling_rate,
            dim=dim,
            repeat=3,
            max_len=20,
        )

    elif audio_transform_enum is AudioTransforms.MEL_SPECTROGRAM_RESIZE_REPEAT:
        return MelSpectrogramResizedRepeated(
            sampling_rate=sampling_rate,
            dim=dim,
            repeat=3,
        )
    elif audio_transform_enum is AudioTransforms.WAV2VEC:
        return AudioTransformWav2Vec2(
            sampling_rate=sampling_rate,
            pretrained_tag=config_defaults.DEFAULT_WAV2VEC_PRETRAINED_TAG,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    raise UnsupportedAudioTransforms(f"Unsupported transform {audio_transform_enum}")