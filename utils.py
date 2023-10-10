from scipy.integrate import odeint
import numpy as np
import torch
import os
import torchvision
import torchvision.transforms as transforms
from torch import nn
from esn import spectral_norm_scaling


class LSTM(nn.Module):
    def __init__(self, n_inp, n_hid, n_out):
        super().__init__()
        self.lstm = torch.nn.LSTM(n_inp, n_hid, batch_first=True,
                                  num_layers=1)
        self.readout = torch.nn.Linear(n_hid, n_out)

    def forward(self, x):
        out, h = self.lstm(x)
        out = self.readout(out[:, -1])
        return out



class RNN_Separate(nn.Module):
    def __init__(self, n_inp, n_hid):
        super().__init__()
        self.i2h = torch.nn.Linear(n_inp, n_hid)
        self.h2h = torch.nn.Linear(n_hid, n_hid)
        self.n_hid = n_hid

    def forward(self, x):
        states = []
        state = torch.zeros(x.size(0), self.n_hid, requires_grad=False).to(x.device)
        for t in range(x.size(1)):
            state = torch.tanh(self.i2h(x[:, t])) + torch.tanh(self.h2h(state))
            states.append(state)
        return torch.stack(states, dim=1), state

class RNN(nn.Module):
    def __init__(self, n_inp, n_hid, n_out, separate_nonlin=False):
        super().__init__()
        if separate_nonlin:
            self.rnn = RNN_Separate(n_inp, n_hid)
        else:
            self.rnn = torch.nn.RNN(n_inp, n_hid, batch_first=True,
                                    num_layers=1)
        self.readout = torch.nn.Linear(n_hid, n_out)

    def forward(self, x):
        out, h = self.rnn(x)
        out = self.readout(out[:, -1])
        return out

class coRNNCell(nn.Module):
    def __init__(self, n_inp, n_hid, dt, gamma, epsilon, no_friction=False, device='cpu'):
        super(coRNNCell, self).__init__()
        self.dt = dt
        gamma_min, gamma_max = gamma
        eps_min, eps_max = epsilon
        self.gamma = torch.rand(n_hid, requires_grad=False, device=device) * (gamma_max - gamma_min) + gamma_min
        self.epsilon = torch.rand(n_hid, requires_grad=False, device=device) * (eps_max - eps_min) + eps_min
        if no_friction:
            self.i2h = nn.Linear(n_inp + n_hid, n_hid)
        else:
            self.i2h = nn.Linear(n_inp + n_hid + n_hid, n_hid)
        self.no_friction = no_friction

    def forward(self,x,hy,hz):
        if self.no_friction:
            i2h_inp = torch.cat((x, hy), 1)
        else:
            i2h_inp = torch.cat((x, hz, hy), 1)
        hz = hz + self.dt * (torch.tanh(self.i2h(i2h_inp))
                             - self.gamma * hy - self.epsilon * hz)
        hy = hy + self.dt * hz

        return hy, hz

class coRNN(nn.Module):
    """
    Batch-first (B, L, I)
    """
    def __init__(self, n_inp, n_hid, n_out, dt, gamma, epsilon, device='cpu',
                 no_friction=False):
        super(coRNN, self).__init__()
        self.n_hid = n_hid
        self.cell = coRNNCell(n_inp,n_hid,dt,gamma,epsilon, no_friction=no_friction, device=device)
        self.readout = nn.Linear(n_hid, n_out)
        self.device = device

    def forward(self, x):
        ## initialize hidden states
        hy = torch.zeros(x.size(0), self.n_hid).to(self.device)
        hz = torch.zeros(x.size(0), self.n_hid).to(self.device)

        for t in range(x.size(1)):
            hy, hz = self.cell(x[:, t],hy,hz)
        output = self.readout(hy)

        return output


class coESN(nn.Module):
    """
    Batch-first (B, L, I)
    """
    def __init__(self, n_inp, n_hid, dt, gamma, epsilon, rho, input_scaling, device='cpu',
                 fading=False):
        super().__init__()
        self.n_hid = n_hid
        self.device = device
        self.fading = fading
        self.dt = dt
        if isinstance(gamma, tuple):
            gamma_min, gamma_max = gamma
            self.gamma = torch.rand(n_hid, requires_grad=False, device=device) * (gamma_max - gamma_min) + gamma_min
        else:
            self.gamma = gamma
        if isinstance(epsilon, tuple):
            eps_min, eps_max = epsilon
            self.epsilon = torch.rand(n_hid, requires_grad=False, device=device) * (eps_max - eps_min) + eps_min
        else:
            self.epsilon = epsilon

        h2h = 2 * (2 * torch.rand(n_hid, n_hid) - 1)
        h2h = spectral_norm_scaling(h2h, rho)
        self.h2h = nn.Parameter(h2h, requires_grad=False)

        x2h = torch.rand(n_inp, n_hid) * input_scaling
        self.x2h = nn.Parameter(x2h, requires_grad=False)
        bias = (torch.rand(n_hid) * 2 - 1) * input_scaling
        self.bias = nn.Parameter(bias, requires_grad=False)

    def cell(self, x, hy, hz):
        hz = hz + self.dt * (torch.tanh(
            torch.matmul(x, self.x2h)  + torch.matmul(hy, self.h2h) + self.bias)
                             - self.gamma * hy - self.epsilon * hz)
        if self.fading:
            hz = hz - self.dt * hz

        hy = hy + self.dt * hz
        if self.fading:
            hy = hy - self.dt * hy
        return hy, hz

    def forward(self, x):
        ## initialize hidden states
        hy = torch.zeros(x.size(0),self.n_hid).to(self.device)
        hz = torch.zeros(x.size(0),self.n_hid).to(self.device)
        all_states = []
        for t in range(x.size(1)):
            hy, hz = self.cell(x[:, t],hy,hz)
            all_states.append(hy)

        return torch.stack(all_states, dim=1), [hy]  # list to be compatible with ESN implementation
        # return None, [hy]  # list to be compatible with ESN implementation



def get_cifar_data(bs_train,bs_test):
    train_dataset = torchvision.datasets.CIFAR10(root='data/',
                                                 train=True,
                                                 transform=transforms.ToTensor(),
                                                 download=True)

    test_dataset = torchvision.datasets.CIFAR10(root='data/',
                                                train=False,
                                                transform=transforms.ToTensor())

    train_dataset, valid_dataset = torch.utils.data.random_split(train_dataset, [47000,3000])

    # Data loader
    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                               batch_size=bs_train,
                                               shuffle=True,
                                               drop_last=True)

    valid_loader = torch.utils.data.DataLoader(dataset=valid_dataset,
                                               batch_size=bs_test,
                                               shuffle=False,
                                               drop_last=True)

    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                              batch_size=bs_test,
                                              shuffle=False,
                                              drop_last=True)

    return train_loader, valid_loader, test_loader

def get_lorenz(N, F, num_batch=128, lag=25, washout=200, window_size=0):
    # https://en.wikipedia.org/wiki/Lorenz_96_model
    def L96(x, t):
        """Lorenz 96 model with constant forcing"""
        # Setting up vector
        d = np.zeros(N)
        # Loops over indices (with operations and Python underflow indexing handling edge cases)
        for i in range(N):
            d[i] = (x[(i + 1) % N] - x[i - 2]) * x[i - 1] - x[i] + F
        return d

    dt = 0.01
    t = np.arange(0.0, 20+(lag*dt)+(washout*dt), dt)
    dataset = []
    for i in range(num_batch):
        x0 = np.random.rand(N) + F - 0.5 # [F-0.5, F+0.5]
        x = odeint(L96, x0, t)
        dataset.append(x)
    dataset = np.stack(dataset, axis=0)
    dataset = torch.from_numpy(dataset).float()

    if window_size > 0:
        windows, targets = [], []
        for i in range(dataset.shape[0]):
            w, t = get_fixed_length_windows(dataset[i], window_size, prediction_lag=lag)
        windows.append(w)
        targets.append(t)
        return torch.utils.data.TensorDataset(torch.cat(windows, dim=0), torch.cat(targets, dim=0))
    else:
        return dataset


def get_mackey_glass(washout=200, window_size=0):
    """
    Predict next-item of mackey-glass series
    """
    with open('mackey-glass.csv', 'r') as f:
        dataset = f.readlines()[0]  # single line file

    # 10k steps
    dataset = torch.tensor([float(el) for el in dataset.split(',')]).float()

    if window_size > 0:
        assert washout == 0
        dataset, targets = get_fixed_length_windows(dataset, window_size, prediction_lag=1)

    end_train = int(dataset.shape[0] / 2)
    end_val = end_train + int(dataset.shape[0] / 4)
    end_test = dataset.shape[0]

    if window_size > 0:
        train_dataset = dataset[:end_train]
        train_target = targets[:end_train]

        val_dataset = dataset[end_train:end_val]
        val_target = targets[end_train:end_val]

        test_dataset = dataset[end_val:end_test]
        test_target = targets[end_val:end_test]
    else:
        train_dataset = dataset[:end_train-1]
        train_target = dataset[washout+1:end_train]

        val_dataset = dataset[end_train:end_val-1]
        val_target = dataset[end_train+washout+1:end_val]

        test_dataset = dataset[end_val:end_test-1]
        test_target = dataset[end_val+washout+1:end_test]

    return (train_dataset, train_target), (val_dataset, val_target), (test_dataset, test_target)


def get_mnist_data(bs_train,bs_test):
    train_dataset = torchvision.datasets.MNIST(root='data/',
                                               train=True,
                                               transform=transforms.ToTensor(),
                                               download=True)

    test_dataset = torchvision.datasets.MNIST(root='data/',
                                              train=False,
                                              transform=transforms.ToTensor())

    train_dataset, valid_dataset = torch.utils.data.random_split(train_dataset, [57000,3000])

    # Data loader
    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                               batch_size=bs_train,
                                               shuffle=True)

    valid_loader = torch.utils.data.DataLoader(dataset=valid_dataset,
                                              batch_size=bs_test,
                                              shuffle=False)

    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                              batch_size=bs_test,
                                              shuffle=False)

    return train_loader, valid_loader, test_loader


def load_har(root):
    """
    Dataset preprocessing code adapted from
    https://github.com/guillaume-chevalier/LSTM-Human-Activity-Recognition/blob/master/LSTM.ipynb
    LABELS = [
        "WALKING",
        "WALKING_UPSTAIRS",
        "WALKING_DOWNSTAIRS",
        "SITTING",
        "STANDING",
        "LAYING"
    ]
    """
    INPUT_SIGNAL_TYPES = [
        "body_acc_x_",
        "body_acc_y_",
        "body_acc_z_",
        "body_gyro_x_",
        "body_gyro_y_",
        "body_gyro_z_",
        "total_acc_x_",
        "total_acc_y_",
        "total_acc_z_"
    ]
    # FROM LABELS IDX (starting from 1) TO BINARY CLASSES (0-1)
    CLASS_MAP = {1: 1, 2: 0, 3: 1, 4: 0, 5: 1, 6: 0}
    TRAIN = "train"
    TEST = "test"

    def load_X(X_signals_paths):
        X_signals = []

        for signal_type_path in X_signals_paths:
            with open(signal_type_path, 'r') as file:
                X_signals.append(
                    [np.array(serie, dtype=np.float32) for serie in [
                        row.replace('  ', ' ').strip().split(' ') for row in file
                    ]]
                )

        return np.transpose(np.array(X_signals), (1, 2, 0))

    def load_y(y_path):
        with open(y_path, 'r') as file:
            y_ = np.array(
                [CLASS_MAP[int(row)] for row in file],
                dtype=np.int32
            )
        return y_


    X_train_signals_paths = [
        os.path.join(root, TRAIN, "Inertial Signals", signal+"train.txt") for signal in INPUT_SIGNAL_TYPES
    ]
    X_test_signals_paths = [
        os.path.join(root, TEST, "Inertial Signals", signal+"test.txt") for signal in INPUT_SIGNAL_TYPES
    ]

    X_train = load_X(X_train_signals_paths)
    X_test = load_X(X_test_signals_paths)

    y_train = load_y(os.path.join(root, TRAIN, "y_train.txt"))
    y_test = load_y(os.path.join(root, TEST, "y_test.txt"))

    train_dataset = torch.utils.data.TensorDataset(torch.from_numpy(X_train).float(), torch.from_numpy(y_train).long())
    test_dataset = torch.utils.data.TensorDataset(torch.from_numpy(X_test).float(), torch.from_numpy(y_test).long())
    val_length = int(len(train_dataset) * 0.3)
    train_dataset, val_dataset = torch.utils.data.random_split(train_dataset, [len(train_dataset)-val_length, val_length])
    return train_dataset, val_dataset, test_dataset


def get_fixed_length_windows(tensor, length, prediction_lag=1):
    assert len(tensor.shape) <= 2
    if len(tensor.shape) == 1:
        tensor = tensor.unsqueeze(-1)

    windows = tensor[:-prediction_lag].unfold(0, length, 1)
    windows = windows.permute(0, 2, 1)

    targets = tensor[length+prediction_lag-1:]
    return windows, targets  # input (B, L, I), target, (B, I)


@torch.no_grad()
def check(m):
    xi = torch.max(torch.abs(1 - m.epsilon * m.dt))
    eta = torch.max(torch.abs(1 - m.gamma * m.dt**2))
    sigma = torch.norm(m.h2h)
    print(xi, eta, sigma, torch.max(m.epsilon), torch.max(m.gamma))

    if (xi - eta) / (m.dt ** 2) <= xi - torch.max(m.gamma):
        if sigma <= (xi - eta) / (m.dt ** 2) and xi < 1 / (1 + m.dt):
            return True
        if (xi - eta) / (m.dt ** 2) < sigma and sigma <= xi - torch.max(m.gamma) and sigma < (1 - xi - eta) / m.dt**2:
            return True
        if sigma >= xi - torch.max(m.gamma) and sigma <= (1 - eta - m.dt * torch.max(m.gamma)) / (m.dt * (1 + m.dt)):
            return True
    else:
        if sigma <= xi - torch.max(m.gamma) and xi < 1 / (1 + m.dt):
            return True
        if xi - torch.max(m.gamma) < sigma and sigma <= (xi - eta) / (m.dt ** 2) and sigma < ((1 - xi) / m.dt) - torch.max(m.gamma):
            return True
        if sigma >= (xi - eta) / m.dt**2 and sigma < (1 - eta - m.dt * torch.max(m.gamma)) / (m.dt * (1 + m.dt)):
            return True
    return False
