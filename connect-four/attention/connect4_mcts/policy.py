"""Contains the classes and functions related to the neural networks used as the policy functions.
"""
from typing import Tuple

import numpy as np
import torch
from torch import nn

from . import game


class ConvLayer(nn.Module):
    """Convolutional layer with normalization and ReLU activation.
    """
    def __init__(self, in_channels: int, out_channels: int):
        """Create a convolutional layer with normalization and ReLU activation.

        Args:
            in_channels (int): The number of input channels.
            out_channels (int): The number of output channels.
        """
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, 3, 1, "same")
        self.norm = nn.BatchNorm2d(out_channels)
        self.act = nn.ReLU()

    def forward(self, x: torch.Tensor): # pylint: disable=invalid-name,missing-function-docstring
        x = self.conv(x)
        x = self.norm(x)
        x = self.act(x)
        return x


class ResidualBlock(nn.Module):
    """Residual layer with two convolutions, then addition. Taken from ResNet architecture.
    """
    def __init__(self, channels: int):
        """Residual layer with two convolutions, then addition.

        Args:
            channels (int): Number of channels in the convolution.
        """
        super().__init__()
        self.conv1 = ConvLayer(channels, channels)
        self.conv2 = nn.Conv2d(channels, channels, 3, 1, "same")
        self.norm2 = nn.BatchNorm2d(channels)

    def forward(self, x: torch.Tensor): # pylint: disable=invalid-name,missing-function-docstring
        shortcut = x
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.norm2(x)
        return shortcut + x


class Network(nn.Module):
    """The network with value and policy heads.
    """
    def __init__(self, channels: int, blocks: int):
        """The network with value and policy heads.

        Args:
            channels (int): The dimensionality of the model.
            blocks (int): The number of residual blocks in the model.
        """
        super().__init__()
        assert game.Game.NUM_ACTIONS == game.Game.STATE_WIDTH
        self.init_conv = ConvLayer(game.Game.STATE_LAYERS, channels)
        self.embed = nn.Sequential(
            *[ResidualBlock(channels) for _ in range(blocks)],
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Flatten(),
        )
        # encoder_layer = nn.TransformerEncoderLayer(channels, 8, 256)
        # self.encoder = nn.TransformerEncoder(encoder_layer, 4)
        self.transformer = nn.Transformer(channels, 8, 2, 2, 256)
        self.policy = nn.Sequential(
            nn.Linear(channels, game.Game.NUM_ACTIONS),
            nn.Softmax(dim=-1),
        )
        self.wdl = nn.Sequential(
            nn.Linear(channels, 3),
            nn.Softmax(dim=-1),
        )

    def forward(self, x: torch.Tensor): # pylint: disable=invalid-name,missing-function-docstring
        # TODO use only encoder and use appropriate src_mask
        x = self.init_conv(x)
        x = self.embed(x)[None, ...]
        x = self.transformer(x, x)
        policy = self.policy(x)
        wdl = self.wdl(x)
        return policy, wdl


class Model:
    """The wrapper class for the network.
    """
    def __init__(
        self,
        channels: int,
        blocks: int,
        learning_rate: float,
        device: str = None,
    ):
        """The wrapper class for the network.

        Args:
            channels (int): The dimensionality of the model.
            blocks (int): The number of residual blocks in the model.
            learning_rate (float): The learning rate of the optimizer.
            device (str, optional): The device to use. Should be one of the torch devices.
            Defaults to 'cpu'.
        """
        self.criterion = nn.MSELoss()
        if device is None:
            device = "cpu"
        self.device = device
        self.net = Network(channels, blocks).to(self.device)
        self.optimizer = torch.optim.Adam(self.net.parameters(), learning_rate)

    @torch.no_grad()
    def policy_function(self, state: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """The policy function. Returns moves and win-draw-lose probabilities.

        Args:
            state (np.ndarray): The game state to be passed into the network.

        Returns:
            Tuple[np.ndarray, np.ndarray]: Policy, WDL
        """
        state = state.astype(np.float32)
        state = torch.from_numpy(state) # pylint: disable=no-member
        act, val = self.net(state) # pylint: disable=not-callable
        act = act.detach().cpu().numpy()
        val = val.detach().cpu().numpy()
        return act[0][-1], val[0][-1]

    def load(self, file_name: str):
        """Loads model weights from the file.

        Args:
            file_name (str): The file name.
        """
        state_dict = torch.load(file_name, map_location=self.device)
        self.net.load_state_dict(state_dict)

    def save(self, file_name: str):
        """Save model weights to the file.

        Args:
            file_name (str): The name of the file.
        """
        torch.save(self.net.state_dict(), file_name)

    def train_step(
        self, states: torch.Tensor, y_pol: torch.Tensor, y_wdl: torch.Tensor
    ) -> float:
        """Makes one training step on given (S, P, W) tuple.

        Args:
            states (torch.Tensor): Game states.
            y_pol (torch.Tensor): Move probabilities.
            y_wdl (torch.Tensor): Win-Draw-Lose probabilities.

        Returns:
            float: Training loss on the given data.
        """
        states = states.to(self.device)
        y_pol = y_pol[None, ...].to(self.device)
        y_wdl = y_wdl[None, ...].to(self.device)
        pred_pol, pred_wdl = self.net(states) # pylint: disable=not-callable
        pol_loss = self.criterion(pred_pol, y_pol)
        wdl_loss = self.criterion(pred_wdl, y_wdl)
        total_loss = pol_loss + wdl_loss
        self.optimizer.zero_grad()
        total_loss.backward()
        self.optimizer.step()
        return total_loss.detach().cpu().numpy()

    def train(
        self, states: np.ndarray, y_pol: np.ndarray, y_wdl: np.ndarray, _batch_size: int
    ) -> float:
        """Train model on the data. The shape of the data is (n, s, h, w), 
        where n is the batch size, and s is the sequence length for this state.

        Args:
            states (np.ndarray): The game states.
            y_pol (np.ndarray): Move probabilities.
            y_wdl (np.ndarray): WDL probabilities.
            batch_size (int): Unused, as the model trains on one sequence at a time.

        Returns:
            float: Mean of the training loss over all tuples.
        """
        losses = []
        for state, pol, wdl in zip(states, y_pol, y_wdl):
            state, pol, wdl = map(torch.from_numpy, (state, pol, wdl)) # pylint: disable=no-member
            loss = self.train_step(state, pol, wdl)
            losses.append(loss)
        return np.mean(losses)
