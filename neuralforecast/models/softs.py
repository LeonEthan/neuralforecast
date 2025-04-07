# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/models.softs.ipynb.

# %% auto 0
__all__ = ['DataEmbedding_inverted', 'STAD', 'SOFTS']

# %% ../../nbs/models.softs.ipynb 4
import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Optional
from ..losses.pytorch import MAE
from ..common._base_model import BaseModel
from ..common._modules import TransEncoder, TransEncoderLayer

# %% ../../nbs/models.softs.ipynb 6
class DataEmbedding_inverted(nn.Module):
    """
    Data Embedding
    """

    def __init__(self, c_in, d_model, dropout=0.1, embed_norm=True):
        super(DataEmbedding_inverted, self).__init__()
        self.embed_norm = embed_norm
        self.value_embedding = nn.Linear(c_in, d_model)
        self.dropout = nn.Dropout(p=dropout)
        self.norm = nn.LayerNorm(d_model) if embed_norm else None

    def forward(self, x, x_mark):
        x = x.permute(0, 2, 1)
        # x: [Batch Variate Time]
        if x_mark is None:
            x = self.value_embedding(x)
        else:
            # the potential to take covariates (e.g. timestamps) as tokens
            x = self.value_embedding(torch.cat([x, x_mark.permute(0, 2, 1)], 1))
        # x: [Batch Variate d_model]
        x = self.norm(x) if self.embed_norm else x
        return self.dropout(x)

# %% ../../nbs/models.softs.ipynb 8
class STAD(nn.Module):
    """
    STar Aggregate Dispatch Module
    """

    def __init__(self, d_series, d_core):
        super(STAD, self).__init__()

        self.gen1 = nn.Linear(d_series, d_series)
        self.gen2 = nn.Linear(d_series, d_core)
        self.gen3 = nn.Linear(d_series + d_core, d_series)
        self.gen4 = nn.Linear(d_series, d_series)

    def forward(self, input, *args, **kwargs):
        batch_size, channels, d_series = input.shape

        # set FFN
        combined_mean = F.gelu(self.gen1(input))
        combined_mean = self.gen2(combined_mean)

        # stochastic pooling
        if self.training:
            ratio = F.softmax(torch.nan_to_num(combined_mean), dim=1)
            ratio = ratio.permute(0, 2, 1)
            ratio = ratio.reshape(-1, channels)
            indices = torch.multinomial(ratio, 1)
            indices = indices.view(batch_size, -1, 1).permute(0, 2, 1)
            combined_mean = torch.gather(combined_mean, 1, indices)
            combined_mean = combined_mean.repeat(1, channels, 1)
        else:
            weight = F.softmax(combined_mean, dim=1)
            combined_mean = torch.sum(
                combined_mean * weight, dim=1, keepdim=True
            ).repeat(1, channels, 1)

        # mlp fusion
        combined_mean_cat = torch.cat([input, combined_mean], -1)
        combined_mean_cat = F.gelu(self.gen3(combined_mean_cat))
        combined_mean_cat = self.gen4(combined_mean_cat)
        output = combined_mean_cat

        return output, None

# %% ../../nbs/models.softs.ipynb 10
class FeatureEmbedding(nn.Module):
    """
    特征融合模块，通过分通道嵌入实现参数控制：
    1. 将原始hidden_size均分给各特征通道
    2. 各特征独立进行嵌入编码
    3. 沿特征维度拼接最终结果
    """

    def __init__(
        self,
        input_size,
        h,
        hidden_size,
        hist_exog_size,
        futr_exog_size,
        stat_exog_size,
        dropout,
        embed_norm,
    ):
        super().__init__()
        self.futr_input_size = input_size + h
        self.futr_exog_size = futr_exog_size
        self.hist_exog_size = hist_exog_size
        self.stat_exog_size = stat_exog_size
        self.base_embed = DataEmbedding_inverted(
            input_size, hidden_size, dropout, embed_norm
        )

        # 历史特征编码器
        self.hist_embed = nn.ModuleList(
            [
                DataEmbedding_inverted(input_size, hidden_size, dropout, embed_norm)
                for _ in range(hist_exog_size)
            ]
        )

        # 未来特征编码器（使用历史部分）
        self.futr_embed = nn.ModuleList(
            [
                DataEmbedding_inverted(
                    self.futr_input_size, hidden_size, dropout, embed_norm
                )
                for _ in range(futr_exog_size)
            ]
        )
        # 静态特征编码（通过线性映射）
        if stat_exog_size > 0:
            layers = [nn.Linear(stat_exog_size, hidden_size)]
            if embed_norm:
                layers.append(nn.BatchNorm1d(hidden_size))
            self.stat_embed = nn.Sequential(*layers)
        else:
            self.stat_embed = None

    def forward(self, y, hist, futr, stat):
        # 基础序列嵌入 [B, N, E]
        embeddings = [self.base_embed(y, None)]

        # 历史特征嵌入 [B, N, E] * H
        if self.hist_exog_size > 0:
            for i, embed in enumerate(self.hist_embed):
                embeddings.append(embed(hist[:, i, :, :], None))

        # 未来特征嵌入 [B, N, E] * F
        if self.futr_exog_size > 0:
            for i, embed in enumerate(self.futr_embed):
                embeddings.append(embed(futr[:, i, :, :], None))

        # 静态特征嵌入 [B, N, E]
        if self.stat_embed is not None:
            stat_feat = self.stat_embed(stat)  # [N, S] -> [N, E]
            stat_feat = stat_feat.unsqueeze(0).expand(y.size(0), -1, -1)  # [B, N, E]
            embeddings.append(stat_feat)

        # 沿特征维度拼接 [B, N, E*(1+H+F+S)]
        return torch.cat(embeddings, dim=-1)

# %% ../../nbs/models.softs.ipynb 12
class SOFTS(BaseModel):
    """SOFTS

    **Parameters:**<br>
    `h`: int, Forecast horizon. <br>
    `input_size`: int, autorregresive inputs size, y=[1,2,3,4] input_size=2 -> y_[t-2:t]=[1,2].<br>
    `n_series`: int, number of time-series.<br>
    `futr_exog_list`: str list, future exogenous columns.<br>
    `hist_exog_list`: str list, historic exogenous columns.<br>
    `stat_exog_list`: str list, static exogenous columns.<br>
    `exclude_insample_y`: bool=False, whether to exclude the target variable from the input.<br>
    `hidden_size`: int, dimension of the model.<br>
    `d_core`: int, dimension of core in STAD.<br>
    `e_layers`: int, number of encoder layers.<br>
    `d_ff`: int, dimension of fully-connected layer.<br>
    `dropout`: float, dropout rate.<br>
    `use_norm`: bool, whether to normalize or not.<br>
    `embed_norm`: bool, whether to apply normalization to various embedding layers.<br>
    `loss`: PyTorch module, instantiated train loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `valid_loss`: PyTorch module=`loss`, instantiated valid loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `max_steps`: int=1000, maximum number of training steps.<br>
    `learning_rate`: float=1e-3, Learning rate between (0, 1).<br>
    `num_lr_decays`: int=-1, Number of learning rate decays, evenly distributed across max_steps.<br>
    `early_stop_patience_steps`: int=-1, Number of validation iterations before early stopping.<br>
    `val_check_steps`: int=100, Number of training steps between every validation loss check.<br>
    `batch_size`: int=32, number of different series in each batch.<br>
    `valid_batch_size`: int=None, number of different series in each validation and test batch, if None uses batch_size.<br>
    `windows_batch_size`: int=32, number of windows to sample in each training batch, default uses all.<br>
    `inference_windows_batch_size`: int=32, number of windows to sample in each inference batch, -1 uses all.<br>
    `start_padding_enabled`: bool=False, if True, the model will pad the time series with zeros at the beginning, by input size.<br>
    `step_size`: int=1, step size between each window of temporal data.<br>
    `scaler_type`: str='identity', type of scaler for temporal inputs normalization see [temporal scalers](https://nixtla.github.io/neuralforecast/common.scalers.html).<br>
    `random_seed`: int=1, random_seed for pytorch initializer and numpy generators.<br>
    `drop_last_loader`: bool=False, if True `TimeSeriesDataLoader` drops last non-full batch.<br>
    `alias`: str, optional,  Custom name of the model.<br>
    `optimizer`: Subclass of 'torch.optim.Optimizer', optional, user specified optimizer instead of the default choice (Adam).<br>
    `optimizer_kwargs`: dict, optional, list of parameters used by the user specified `optimizer`.<br>
    `lr_scheduler`: Subclass of 'torch.optim.lr_scheduler.LRScheduler', optional, user specified lr_scheduler instead of the default choice (StepLR).<br>
    `lr_scheduler_kwargs`: dict, optional, list of parameters used by the user specified `lr_scheduler`.<br>
    `dataloader_kwargs`: dict, optional, list of parameters passed into the PyTorch Lightning dataloader by the `TimeSeriesDataLoader`. <br>
    `**trainer_kwargs`: int,  keyword trainer arguments inherited from [PyTorch Lighning's trainer](https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.trainer.trainer.Trainer.html?highlight=trainer).<br>

    **References**<br>
    [Lu Han, Xu-Yang Chen, Han-Jia Ye, De-Chuan Zhan. "SOFTS: Efficient Multivariate Time Series Forecasting with Series-Core Fusion"](https://arxiv.org/pdf/2404.14197)
    """

    # Class attributes
    EXOGENOUS_FUTR = True
    EXOGENOUS_HIST = True
    EXOGENOUS_STAT = True
    MULTIVARIATE = True
    RECURRENT = False

    def __init__(
        self,
        h,
        input_size,
        n_series,
        futr_exog_list=None,
        hist_exog_list=None,
        stat_exog_list=None,
        exclude_insample_y=False,
        hidden_size: int = 512,
        d_core: int = 512,
        e_layers: int = 2,
        d_ff: int = 2048,
        dropout: float = 0.1,
        use_norm: bool = True,
        embed_norm: bool = True,
        loss=MAE(),
        valid_loss=None,
        max_steps: int = 1000,
        learning_rate: float = 1e-3,
        num_lr_decays: int = -1,
        early_stop_patience_steps: int = -1,
        val_check_steps: int = 100,
        batch_size: int = 32,
        valid_batch_size: Optional[int] = None,
        windows_batch_size=32,
        inference_windows_batch_size=32,
        start_padding_enabled=False,
        step_size: int = 1,
        scaler_type: str = "identity",
        random_seed: int = 1,
        drop_last_loader: bool = False,
        alias: Optional[str] = None,
        optimizer=None,
        optimizer_kwargs=None,
        lr_scheduler=None,
        lr_scheduler_kwargs=None,
        dataloader_kwargs=None,
        **trainer_kwargs
    ):

        super(SOFTS, self).__init__(
            h=h,
            input_size=input_size,
            n_series=n_series,
            futr_exog_list=futr_exog_list,
            hist_exog_list=hist_exog_list,
            stat_exog_list=stat_exog_list,
            exclude_insample_y=exclude_insample_y,
            loss=loss,
            valid_loss=valid_loss,
            max_steps=max_steps,
            learning_rate=learning_rate,
            num_lr_decays=num_lr_decays,
            early_stop_patience_steps=early_stop_patience_steps,
            val_check_steps=val_check_steps,
            batch_size=batch_size,
            valid_batch_size=valid_batch_size,
            windows_batch_size=windows_batch_size,
            inference_windows_batch_size=inference_windows_batch_size,
            start_padding_enabled=start_padding_enabled,
            step_size=step_size,
            scaler_type=scaler_type,
            random_seed=random_seed,
            drop_last_loader=drop_last_loader,
            alias=alias,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            lr_scheduler=lr_scheduler,
            lr_scheduler_kwargs=lr_scheduler_kwargs,
            dataloader_kwargs=dataloader_kwargs,
            **trainer_kwargs
        )

        self.h = h
        self.enc_in = n_series
        self.dec_in = n_series
        self.c_out = n_series
        self.use_norm = use_norm

        # Architecture
        # Mix all features into one
        self.num_features = (
            1
            + (len(hist_exog_list) if hist_exog_list else 0)
            + (len(futr_exog_list) if futr_exog_list else 0)
            + 1
        )
        adjusted_hidden = hidden_size // self.num_features
        self.hidden_size = adjusted_hidden * self.num_features
        self.feature_embedding = FeatureEmbedding(
            input_size=input_size,
            h=h,
            hidden_size=adjusted_hidden,
            hist_exog_size=len(hist_exog_list) if hist_exog_list else 0,
            futr_exog_size=len(futr_exog_list) if futr_exog_list else 0,
            stat_exog_size=len(stat_exog_list) if stat_exog_list else 0,
            dropout=dropout,
            embed_norm=embed_norm,
        )

        self.encoder = TransEncoder(
            [
                TransEncoderLayer(
                    STAD(self.hidden_size, d_core),
                    self.hidden_size,
                    d_ff,
                    dropout=dropout,
                    activation=F.gelu,
                )
                for l in range(e_layers)
            ]
        )

        self.projection = nn.Linear(
            self.hidden_size, self.h * self.loss.outputsize_multiplier, bias=True
        )

    def forecast(self, x_enc, hist_exog, futr_exog, stat_exog):
        # Normalization from Non-stationary Transformer
        if self.use_norm:
            means = x_enc.mean(1, keepdim=True).detach()
            x_enc = x_enc - means
            stdev = torch.sqrt(
                torch.var(x_enc, dim=1, keepdim=True, unbiased=False) + 1e-5
            )
            x_enc /= stdev

        _, _, N = x_enc.shape
        enc_out = self.feature_embedding(x_enc, hist_exog, futr_exog, stat_exog)
        enc_out, attns = self.encoder(enc_out, attn_mask=None)
        dec_out = self.projection(enc_out).permute(0, 2, 1)[:, :, :N]

        # De-Normalization from Non-stationary Transformer
        if self.use_norm:
            dec_out = dec_out * (
                stdev[:, 0, :]
                .unsqueeze(1)
                .repeat(1, self.h * self.loss.outputsize_multiplier, 1)
            )
            dec_out = dec_out + (
                means[:, 0, :]
                .unsqueeze(1)
                .repeat(1, self.h * self.loss.outputsize_multiplier, 1)
            )
        return dec_out

    def forward(self, windows_batch):
        insample_y = windows_batch[
            "insample_y"
        ]  #   [batch_size (B), input_size (L), n_series (N)]
        hist_exog = windows_batch["hist_exog"]  #   [B, hist_exog_size (X), L, N]
        futr_exog = windows_batch["futr_exog"]  #   [B, futr_exog_size (F), L + h, N]
        stat_exog = windows_batch["stat_exog"]  #   [N, stat_exog_size (S)]

        y_pred = self.forecast(insample_y, hist_exog, futr_exog, stat_exog)
        y_pred = y_pred.reshape(insample_y.shape[0], self.h, -1)

        return y_pred
