import torch
import torch.nn as nn

from opacus import PrivacyEngine

# from torch.distributions.bernoulli import Bernoulli
from torch.distributions.normal import Normal

from tqdm import tqdm


class Encoder(nn.Module):
    """Encoder, takes in x
    and outputs mu_z, sigma_z
    (diagonal Gaussian variational posterior assumed)
    """

    def __init__(
        self,
        input_dim,
        latent_dim,
        hidden_dim=32,
        activation=nn.Tanh,
        device="cpu",
    ):
        super().__init__()
        if device == "gpu":
            self.device = torch.device(
                "cuda:0" if torch.cuda.is_available() else "cpu"
            )
            print(f"Encoder: {device} specified, {self.device} used")
        else:
            self.device = torch.device("cpu")
            print(f"Encoder: {device} specified, {self.device} used")
        output_dim = 2 * latent_dim
        self.latent_dim = latent_dim
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            activation(),
            nn.Linear(hidden_dim, hidden_dim),
            activation(),
            nn.Linear(hidden_dim, output_dim),
        )

    def forward(self, x):
        outs = self.net(x)
        mu_z = outs[:, : self.latent_dim]
        logsigma_z = outs[:, self.latent_dim :]
        return mu_z, logsigma_z


class Decoder(nn.Module):
    """Decoder, takes in z and outputs reconstruction"""

    def __init__(
        self,
        latent_dim,
        num_continuous,
        num_categories=[0],
        hidden_dim=32,
        activation=nn.Tanh,
        device="cpu",
    ):
        super().__init__()

        output_dim = num_continuous + sum(num_categories)
        self.num_continuous = num_continuous
        self.num_categories = num_categories

        if device == "gpu":
            self.device = torch.device(
                "cuda:0" if torch.cuda.is_available() else "cpu"
            )
            print(f"Decoder: {device} specified, {self.device} used")
        else:
            self.device = torch.device("cpu")
            print(f"Decoder: {device} specified, {self.device} used")

        self.net = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            activation(),
            nn.Linear(hidden_dim, hidden_dim),
            activation(),
            nn.Linear(hidden_dim, output_dim),
        )

    def forward(self, z):
        return self.net(z)


class Noiser(nn.Module):
    def __init__(self, num_continuous):
        super().__init__()
        self.output_logsigma_fn = nn.Linear(
            num_continuous, num_continuous, bias=True
        )
        torch.nn.init.zeros_(self.output_logsigma_fn.weight)
        torch.nn.init.zeros_(self.output_logsigma_fn.bias)
        self.output_logsigma_fn.weight.requires_grad = False

    def forward(self, X):
        return self.output_logsigma_fn(X)


class VAE(nn.Module):
    """Combines encoder and decoder into full VAE model"""

    def __init__(self, encoder, decoder, lr=1e-3):
        super().__init__()
        self.encoder = encoder.to(encoder.device)
        self.decoder = decoder.to(decoder.device)
        self.num_categories = self.decoder.num_categories
        self.num_continuous = self.decoder.num_continuous
        self.noiser = Noiser(self.num_continuous).to(decoder.device)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        self.lr = lr

    def reconstruct(self, X):
        mu_z, logsigma_z = self.encoder(X)

        x_recon = self.decoder(mu_z)
        return x_recon

    def generate(self, N):
        z_samples = torch.randn_like(torch.ones((N, self.encoder.latent_dim)))
        x_gen = self.decoder(z_samples)
        x_gen_ = torch.ones_like(x_gen)
        i = 0
        for v in range(len(self.num_categories)):
            x_gen_[
                :, i : (i + self.num_categories[v])
            ] = torch.distributions.one_hot_categorical.OneHotCategorical(
                logits=x_gen[:, i : (i + self.num_categories[v])]
            ).sample()
            i = i + self.num_categories[v]
        x_gen_[:, -self.num_continuous :] = x_gen[
            :, -self.num_continuous :
        ] + torch.exp(
            self.noiser(x_gen[:, -self.num_continuous :])
        ) * torch.randn_like(
            x_gen[:, -self.num_continuous :]
        )
        return x_gen_

    def loss(self, X):
        mu_z, logsigma_z = self.encoder(X)

        p = Normal(torch.zeros_like(mu_z), torch.ones_like(mu_z))
        q = Normal(mu_z, torch.exp(logsigma_z))

        encoder_loss = torch.sum(torch.distributions.kl_divergence(q, p))

        s = torch.randn_like(mu_z)
        z_samples = mu_z + s * torch.exp(logsigma_z)

        x_recon = self.decoder(z_samples)

        categoric_loglik = 0
        if sum(self.num_categories) != 0:
            i = 0
            for v in range(len(self.num_categories)):
                categoric_loglik += -torch.nn.functional.cross_entropy(
                    x_recon[:, i : (i + self.num_categories[v])],
                    torch.max(X[:, i : (i + self.num_categories[v])], 1)[1],
                ).sum()
                i = i + self.decoder.num_categories[v]

        gauss_loglik = 0
        if self.decoder.num_continuous != 0:
            gauss_loglik = (
                Normal(
                    loc=x_recon[:, -self.num_continuous :],
                    scale=torch.exp(
                        self.noiser(x_recon[:, -self.num_continuous :])
                    ),
                )
                .log_prob(X[:, -self.num_continuous :])
                .sum()
            )

        reconstruct_loss = -(categoric_loglik + gauss_loglik)

        return encoder_loss + reconstruct_loss

    def train(self, x_dataloader, n_epochs, logging_freq=1):
        # mean_norm = 0
        # counter = 0
        for epoch in range(n_epochs):
            train_loss = 0.0

            for batch_idx, (Y_subset,) in enumerate(tqdm(x_dataloader)):
                self.optimizer.zero_grad()
                loss = self.loss(Y_subset.to(self.encoder.device))
                loss.backward()
                self.optimizer.step()

                train_loss += loss.item()

                # counter += 1
                # l2_norm = 0
                # for p in self.parameters():
                #     if p.requires_grad:
                #         p_norm = p.grad.detach().data.norm(2)
                #         l2_norm += p_norm.item() ** 2
                # l2_norm = l2_norm ** 0.5  # / Y_subset.shape[0]
                # mean_norm = (mean_norm * (counter - 1) + l2_norm) / counter

            if epoch % logging_freq == 0:
                print(f"\tEpoch: {epoch:2}. Total loss: {train_loss:11.2f}")
                # print(f"\tMean norm: {mean_norm}")
        # self.mean_norm = mean_norm

    def diff_priv_train(
        self,
        x_dataloader,
        n_epochs,
        C=1e16,
        noise_scale=None,
        target_eps=1,
        target_delta=1e-5,
        logging_freq=1,
        sample_rate=0.1,
    ):
        if noise_scale is not None:
            self.privacy_engine = PrivacyEngine(
                self,
                sample_rate=sample_rate,
                alphas=[1 + x / 10.0 for x in range(1, 100)]
                + list(range(12, 64)),
                noise_multiplier=noise_scale,
                max_grad_norm=C,
            )
        else:
            self.privacy_engine = PrivacyEngine(
                self,
                sample_rate=sample_rate,
                alphas=[1 + x / 10.0 for x in range(1, 100)]
                + list(range(12, 64)),
                target_epsilon=target_eps,
                target_delta=target_delta,
                epochs=n_epochs,
                max_grad_norm=C,
            )
        self.privacy_engine.attach(self.optimizer)

        for epoch in range(n_epochs):
            train_loss = 0.0
            # print(self.get_privacy_spent(target_delta))

            for batch_idx, (Y_subset,) in enumerate(tqdm(x_dataloader)):
                self.optimizer.zero_grad()
                loss = self.loss(Y_subset.to(self.encoder.device))
                loss.backward()
                self.optimizer.step()
                train_loss += loss.item()
                # print(self.get_privacy_spent(target_delta))
                # print(loss.item())

            if epoch % logging_freq == 0:
                print(f"\tEpoch: {epoch:2}. Total loss: {train_loss:11.2f}")

    def get_privacy_spent(self, delta):
        if hasattr(self, "privacy_engine"):
            return self.privacy_engine.get_privacy_spent(delta)
        else:
            print(
                """This VAE object does not a privacy_engine attribute.
                Run diff_priv_train to create one."""
            )

    def save(self, filename):
        torch.save(self.state_dict(), filename)

    def load(self, filename):
        self.load_state_dict(torch.load(filename))
