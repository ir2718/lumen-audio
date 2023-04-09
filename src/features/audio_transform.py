from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

import librosa
import numpy as np
import torch
import torch_audiomentations
import torchvision.transforms.functional as F
from torchaudio.transforms import FrequencyMasking, TimeMasking
from torchvision.transforms import RandomErasing
from transformers import ASTFeatureExtractor, Wav2Vec2FeatureExtractor

import src.config.config_defaults as config_defaults
from src.features.supported_augmentations import (
    AudioTransforms,
    SupportedAugmentations,
    UnsupportedAudioTransforms,
)
from src.utils.utils_audio import load_audio_from_file, stereo_to_mono, time_stretch


class AudioTransformBase(ABC):
    """Base class for all audio transforms. Ideally, each audio transform class should be self
    contained and shouldn't depened on the outside context.

    Audio transfrom can be model dependent. We can create audio transforms which work only for one
    model and that's fine.
    """

    def __init__(
        self,
        sampling_rate: int,
        augmentation_enums: list[SupportedAugmentations],
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
    ) -> tuple[torch.Tensor]:
        """Function which prepares everything for model's .forward() function. It creates the
        spectrogram from audio.

        Args:
            audio: audio data

        Returns:
            tuple[torch.Tensor, torch.Tensor]: _description_
        """

    def process_from_file(
        self,
        audio_file_path: Path,
        method: Literal["torch", "librosa"],
        normalize: bool,
    ) -> tuple[torch.Tensor]:
        """Calls the process() but loads the file beforehand.

        Args:
            audio_path: audio file path

        Returns:
            tuple[torch.Tensor, torch.Tensor]: _description_
        """
        audio, _ = load_audio_from_file(
            audio_file_path,
            method=method,
            normalize=normalize,
            target_sr=self.sampling_rate,
        )
        return self.process(audio)


class MFCC(AudioTransformBase):
    """Resamples audio, converts it to mono, calculates MFCC (mel-frequency cepstral coefficients)
    from audio."""

    def __init__(
        self,
        n_mfcc: int,
        dct_type: int,
        n_fft: int = config_defaults.DEFAULT_N_FFT,
        hop_length: int = config_defaults.DEFAULT_HOP_LENGTH,
        n_mels: int = config_defaults.DEFAULT_N_MELS,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.n_mfcc = n_mfcc
        self.dct_type = dct_type
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        spectrogram = librosa.feature.mfcc(
            y=audio,
            sr=self.sampling_rate,
            n_mfcc=self.n_mfcc,
            dct_type=self.dct_type,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            n_mels=self.n_mels,
        )

        return spectrogram


class MFCCFixed(MFCC):
    """Resamples audio, extracts MFCC from audio and pads the original spectrogram to dimension of
    spectrogram for max_len sequence."""

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

        spectrogram = librosa.feature.mfcc(
            y=audio_resampled,
            sr=self.sampling_rate,
            n_mfcc=self.n_mfcc,
            dct_type=self.dct_type,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            n_mels=self.n_mels,
        )

        self.seq_dim = spectrogram.shape

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
    ) -> tuple[torch.Tensor]:
        spectrogram = super().process(audio)
        chunks = list(torch.tensor(spectrogram).split(self.seq_dim[1], dim=1))

        # if theyre not exact padding is added for last chunk
        if not spectrogram.shape[1] % self.seq_dim[1] == 0:
            w, h = chunks[-1].shape
            chunk_padded = torch.zeros(size=self.seq_dim)
            chunk_padded[:w, :h] = chunks[-1]
            chunks[-1] = chunk_padded

        for i, c in enumerate(chunks):
            chunks[i] = chunks[i].reshape(1, 1, *self.seq_dim)
            chunks[i] = F.resize(chunks[i], size=self.dim, antialias=True)[0][0]
            chunks[i] = chunks[i].type(torch.float32)

        return chunks


class MFCCFixedRepeat(MFCCFixed):
    """Calls MFCC and repeats the output 3 times.

    This is useful for mocking RGB channels.
    """

    def __init__(self, repeat=3, **kwargs):
        super().__init__(**kwargs)
        self.repeat = repeat

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        spectrogram_chunks = super().process(audio)
        for i, c in enumerate(spectrogram_chunks):
            spectrogram_chunks[i] = spectrogram_chunks[i].repeat(1, self.repeat, 1, 1)[
                0
            ]

        return spectrogram_chunks


######################################################################################################################################################


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
        self.color_noise = torch_audiomentations.AddColoredNoise(
            min_snr_in_db=3,
            max_snr_in_db=25,
            p=1,
            sample_rate=self.sampling_rate,
        )
        self.bandpass_filter = torch_audiomentations.BandPassFilter(
            p=1, sample_rate=self.sampling_rate
        )
        self.pitch_shift = torch_audiomentations.PitchShift(
            p=1, sample_rate=self.sampling_rate
        )
        self.timeinv = torch_audiomentations.TimeInversion(
            p=0.5, sample_rate=self.sampling_rate
        )

    def apply_waveform_augmentations(self, audio):
        if SupportedAugmentations.TIME_STRETCH in self.augmentation_enums:
            l, r = self.stretch_factors
            stretch_rate = np.random.uniform(l, r)
            # size_before = len(audio)
            audio = librosa.effects.time_stretch(y=audio, rate=stretch_rate)
            # audio = audio[:size_before]

        audio = torch.tensor(audio[np.newaxis, np.newaxis, :])

        if SupportedAugmentations.PITCH in self.augmentation_enums:
            audio = self.pitch_shift(audio)

        if SupportedAugmentations.BANDPASS_FILTER in self.augmentation_enums:
            audio = self.bandpass_filter(audio)

        if SupportedAugmentations.COLOR_NOISE in self.augmentation_enums:
            # if min_snr_in_db and max_snr_in_db are lower than the noise is louder
            audio = self.color_noise(audio)

        if SupportedAugmentations.TIMEINV in self.augmentation_enums:
            audio = self.timeinv(audio)

        return audio[0, 0, :].numpy()  # same as x2 squeeze(0)

    def apply_spec_augmentations(self, spectrogram: torch.Tensor):
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
    ) -> tuple[torch.Tensor, torch.Tensor]:
        # TODO: extract this function out
        audio = self.apply_waveform_augmentations(audio)

        spectrogram = self.feature_extractor(
            audio,
            sampling_rate=self.sampling_rate,
            return_tensors="pt",
        )["input_values"]

        assert (
            len(spectrogram.shape) == 3
        ), "Spectrogram has to have 3 dimensions before torch augmentations!"
        spectrogram = self.apply_spec_augmentations(spectrogram)

        # TODO: chunking
        spectrogram_chunks = spectrogram
        assert len(spectrogram_chunks.shape) == 3, "Spectrogram chunks are 2D images"
        return spectrogram_chunks


######################################################################################################################################################


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
    ) -> torch.Tensor:
        spectrogram = librosa.feature.melspectrogram(
            y=audio,
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
    ) -> tuple[torch.Tensor, torch.Tensor]:
        spectrogram = super().process(audio)

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
    ) -> tuple[torch.Tensor]:
        spectrogram = super().process(audio)
        chunks = list(torch.tensor(spectrogram).split(self.seq_dim[1], dim=1))

        # if theyre not exact padding is added for last chunk
        if not spectrogram.shape[1] % self.seq_dim[1] == 0:
            w, h = chunks[-1].shape
            chunk_padded = torch.zeros(size=self.seq_dim)
            chunk_padded[:w, :h] = chunks[-1]
            chunks[-1] = chunk_padded

        for i, c in enumerate(chunks):
            chunks[i] = chunks[i].reshape(1, 1, *self.seq_dim)
            chunks[i] = F.resize(chunks[i], size=self.dim, antialias=True)[0][0]
            chunks[i] = chunks[i].type(torch.float32)

        return chunks


class MelSpectrogramFixedRepeat(MelSpectrogramFixed):
    """Calls MelSpectrogramFixed and repeats the output 3 times.

    This is useful for mocking RGB channels.
    """

    def __init__(self, repeat=3, **kwargs):
        super().__init__(**kwargs)
        self.repeat = repeat

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        spectrogram_chunks = super().process(audio)
        for i, c in enumerate(spectrogram_chunks):
            spectrogram_chunks[i] = spectrogram_chunks[i].repeat(1, self.repeat, 1, 1)[
                0
            ]

        return spectrogram_chunks


class MelSpectrogramResizedRepeat(MelSpectrogramResize):
    """Calls MelSpectrogramResize and repeats the output 3 times.

    This is useful for mocking RGB channels.
    """

    def __init__(self, repeat=3, **kwargs):
        super().__init__(**kwargs)
        self.repeat = repeat

    def process(
        self,
        audio: torch.Tensor | np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        spectrogram = super().process(audio)
        spectrogram = spectrogram.repeat(1, self.repeat, 1, 1)[0]

        return spectrogram


######################################################################################################################################################


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
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if SupportedAugmentations.TIME_STRETCH in self.augmentation_enums:
            min_s, max_s = self.stretch_factors
            audio = time_stretch(audio, min_s, max_s, trim=True)

        audio = librosa.util.fix_length(audio, size=self.sampling_rate * 3)

        features_dict = self.feature_extractor(
            audio,
            sampling_rate=self.sampling_rate,
            return_tensors="pt",
            padding=True,
        )
        features = features_dict.input_values.squeeze(0)

        return features


######################################################################################################################################################


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
        return MelSpectrogramFixedRepeat(
            sampling_rate=sampling_rate,
            dim=dim,
            repeat=config_defaults.DEFAULT_REPEAT,
            max_len=config_defaults.DEFAULT_MAX_LEN,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    elif audio_transform_enum is AudioTransforms.MEL_SPECTROGRAM_RESIZE_REPEAT:
        return MelSpectrogramResizedRepeat(
            sampling_rate=sampling_rate,
            dim=dim,
            repeat=config_defaults.DEFAULT_REPEAT,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    elif audio_transform_enum is AudioTransforms.WAV2VEC:
        return AudioTransformWav2Vec2(
            sampling_rate=sampling_rate,
            pretrained_tag=config_defaults.DEFAULT_WAV2VEC_PRETRAINED_TAG,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    elif audio_transform_enum is AudioTransforms.MFCC_FIXED_REPEAT:
        return MFCCFixedRepeat(
            sampling_rate=sampling_rate,
            dim=dim,
            n_mfcc=config_defaults.DEFAULT_N_MFCC,
            dct_type=config_defaults.DEFAULT_DCT_TYPE,
            repeat=config_defaults.DEFAULT_REPEAT,
            n_fft=config_defaults.DEFAULT_N_FFT,
            hop_length=config_defaults.DEFAULT_HOP_LENGTH,
            n_mels=config_defaults.DEFAULT_N_MELS,
            max_len=config_defaults.DEFAULT_MAX_LEN,
            augmentation_enums=augmentation_enums,
            **aug_kwargs,
        )
    raise UnsupportedAudioTransforms(f"Unsupported transform {audio_transform_enum}")
