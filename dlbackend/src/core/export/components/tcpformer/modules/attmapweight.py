import torch
import torch.nn as nn


class AttWeight(nn.Module):

    def __init__(self, act_layer=nn.GELU, drop=0.):
        super().__init__()
        self.act = act_layer()
        self.drop = nn.Dropout(drop)
        self.fc1 = nn.Linear(16, 1)
        self.fc2 = nn.Linear(567, 243)
        self.sig = nn.Sigmoid()

    def forward(self, x, y, z):
        b, t, j, c = x.shape
        t_sup = z.shape[1]
        h = 8
        x = x.reshape(b, t, j, h, -1)
        y = y.reshape(b, t, j, h, -1)
        z = z.reshape(b, t_sup, j, h, -1)
        sum = torch.cat([x, y, z], dim=1).permute(0, 3, 2, 1, 4)
        sum = self.fc1(sum)
        sum = self.act(sum)
        sum = self.drop(sum)
        sum = sum.reshape(b, h, j, -1)

        sum = self.fc2(sum)
        sum = self.act(sum)
        sum = self.drop(sum)
        sum = sum.reshape(b, h, j, t, 1)

        weight = self.sig(sum)

        return weight
