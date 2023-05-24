import numpy as np
from torch import nn, optim
import torch
import network
import torch.nn.utils
import get_lorenz
from pathlib import Path
import argparse
from esn import DeepReservoir
from sklearn import preprocessing
from sklearn.linear_model import Ridge


parser = argparse.ArgumentParser(description='training parameters')

parser.add_argument('--n_hid', type=int, default=256,
                    help='hidden size of recurrent net')
parser.add_argument('--epochs', type=int, default=120,
                    help='max epochs')
parser.add_argument('--batch', type=int, default=120,
                    help='batch size')
parser.add_argument('--lr', type=float, default=0.0054,
                    help='learning rate')
parser.add_argument('--dt', type=float, default=0.076,
                    help='step size <dt> of the coRNN')
parser.add_argument('--gamma', type=float, default=0.4,
                    help='y controle parameter <gamma> of the coRNN')
parser.add_argument('--epsilon', type=float, default=8.0,
                    help='z controle parameter <epsilon> of the coRNN')
parser.add_argument('--gamma_range', type=float, default=2.7,
                    help='y controle parameter <gamma> of the coRNN')
parser.add_argument('--epsilon_range', type=float, default=4.7,
                    help='z controle parameter <epsilon> of the coRNN')
parser.add_argument('--cpu', action="store_true")
parser.add_argument('--no_friction', action="store_true")
parser.add_argument('--esn', action="store_true")
parser.add_argument('--inp_scaling', type=float, default=1.,
                    help='ESN input scaling')
parser.add_argument('--rho', type=float, default=0.99,
                    help='ESN spectral radius')
parser.add_argument('--leaky', type=float, default=1.0,
                    help='ESN spectral radius')
parser.add_argument('--alpha', type=float, default=0.0)


args = parser.parse_args()
print(args)

main_folder = 'result_leaky'

device = torch.device("cuda") if torch.cuda.is_available() and not args.cpu else torch.device("cpu")
print("Using device ", device)
n_inp = 5
n_out = 5
washout = 200
lag = 25

gamma = (args.gamma - args.gamma_range / 2., args.gamma + args.gamma_range / 2.)
epsilon = (args.epsilon - args.epsilon_range / 2., args.epsilon + args.epsilon_range / 2.)

if args.esn and not args.no_friction:
    model = DeepReservoir(n_inp, tot_units=args.n_hid, spectral_radius=args.rho,
                          input_scaling=args.inp_scaling,
                          connectivity_recurrent=args.n_hid,
                          connectivity_input=args.n_hid, leaky=args.leaky).to(device)
elif args.esn and args.no_friction:

    model = network.coESN(n_inp, args.n_hid, args.dt, gamma, epsilon, args.rho,
                          args.inp_scaling, device=device).to(device)
else:
    model = network.coRNN(n_inp, args.n_hid, n_out,args.dt,gamma,epsilon,
                          no_friction=args.no_friction, device=device).to(device)

train_dataset = get_lorenz.get_lorenz(5, 8, 128)
valid_dataset = get_lorenz.get_lorenz(5, 8, 128)
test_dataset = get_lorenz.get_lorenz(5, 8, 128)

objective = nn.MSELoss()
optimizer = optim.Adam(model.parameters(), lr=args.lr)

@torch.no_grad()
def test_esn(dataset, classifier, scaler):
    target = dataset[(lag+washout):].permute(1, 0, 2).numpy().reshape(-1, 5)
    dataset = dataset[:(2000+washout)].to(device)
    if not args.no_friction:
        dataset = dataset.permute(1, 0, 2)
    activations = model(dataset)[0].cpu().numpy()
    activations = activations[:, washout:]
    activations = activations.reshape(-1, args.n_hid)
    activations = scaler.transform(activations)
    predictions = classifier.predict(activations)
    mse = np.mean(np.square(predictions - target))
    rmse = np.sqrt(mse)
    norm = np.sqrt(np.square(target).mean())
    nrmse = rmse / (norm + 1e-9)
    return nrmse

if args.esn:
    target = train_dataset[(lag+washout):].permute(1, 0, 2).numpy().reshape(-1, 5)
    dataset = train_dataset[:(2000+washout)].to(device)
    if not args.no_friction:
        dataset = dataset.permute(1, 0, 2)
    activations = model(dataset)[0].cpu().numpy()
    activations = activations[:, washout:]
    activations = activations.reshape(-1, args.n_hid)
    scaler = preprocessing.StandardScaler().fit(activations)
    activations = scaler.transform(activations)
    classifier = Ridge(alpha=args.alpha, max_iter=1000).fit(activations, target)
    valid_nmse = test_esn(valid_dataset, classifier, scaler)
    test_nmse = test_esn(test_dataset, classifier, scaler)
else:
    pass

if args.no_friction and (not args.esn): # coRNN without friction
    f = open(f'{main_folder}/lorenz_log_no_friction.txt', 'a')
elif args.esn and args.no_friction: # coESN
    f = open(f'{main_folder}/lorenz_log_coESN.txt', 'a')
elif args.esn: # ESN
    f = open(f'{main_folder}/lorenz_log_esn.txt', 'a')
else: # original coRNN
    f = open(f'{main_folder}/lorenz_log.txt', 'a')
ar = ''
for k, v in vars(args).items():
    ar += f'{str(k)}: {str(v)}, '
ar += f'valid: {str(round(valid_nmse, 5))}, test: {str(round(test_nmse, 5))}'
f.write(ar + '\n')
f.write('**************\n\n\n')
f.close()