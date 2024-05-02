from .earlytrain import EarlyTrain
import torch, time
import numpy as np
from ..nets.nets_utils import MyDataParallel
from utils import *
from backpack import backpack, extend
from backpack.extensions import BatchGrad, DiagHessian, BatchDiagHessian
from collections import OrderedDict
import os

class GraNd(EarlyTrain):
    def __init__(self, dst_train, args, fraction=0.5, random_seed=None, epochs=200, repeat=10,
                 specific_model=None, balance=False, **kwargs):
        super().__init__(dst_train, args, fraction, random_seed, epochs, specific_model)
        self.epochs = epochs
        self.n_train = len(dst_train)
        self.coreset_size = round(self.n_train * fraction)
        self.specific_model = specific_model
        self.repeat = repeat

        self.balance = balance

    def while_update(self, outputs, loss, targets, epoch, batch_idx, batch_size):
        if batch_idx % self.args.print_freq == 0:
            print('| Epoch [%3d/%3d] Iter[%3d/%3d]\t\tLoss: %.4f' % (
                epoch, self.epochs, batch_idx + 1, (self.n_train // batch_size) + 1, loss.item()))

    def before_run(self):
        if isinstance(self.model, MyDataParallel):
            self.model = self.model.module

    def finish_run(self):
        self.model.embedding_recorder.record_embedding = True  # recording embedding vector

        self.model.eval()

        embedding_dim = self.model.get_last_layer().in_features
        batch_loader = torch.utils.data.DataLoader(
            self.dst_train, batch_size=self.args.selection_batch, num_workers=self.args.workers)
        sample_num = self.n_train

        for i, (input, targets) in enumerate(batch_loader):
            self.model_optimizer.zero_grad()
            outputs = self.model(input.to(self.args.device))
            loss = self.criterion(torch.nn.functional.softmax(outputs.requires_grad_(True), dim=1),
                                  targets.to(self.args.device)).sum()
            batch_num = targets.shape[0]
            with torch.no_grad():
                bias_parameters_grads = torch.autograd.grad(loss, outputs)[0]
                self.norm_matrix[i * self.args.selection_batch:min((i + 1) * self.args.selection_batch, sample_num),
                self.cur_repeat] = torch.norm(torch.cat([bias_parameters_grads, (
                        self.model.embedding_recorder.embedding.view(batch_num, 1, embedding_dim).repeat(1,
                                             self.args.num_classes, 1) * bias_parameters_grads.view(
                                             batch_num, self.args.num_classes, 1).repeat(1, 1, embedding_dim)).
                                             view(batch_num, -1)], dim=1), dim=1, p=2)

        self.model.train()

        self.model.embedding_recorder.record_embedding = False

    def select(self, **kwargs):
        # Initialize a matrix to save norms of each sample on idependent runs
        self.norm_matrix = torch.zeros([self.n_train, self.repeat], requires_grad=False).to(self.args.device)

        for self.cur_repeat in range(self.repeat):
            self.run()
            self.random_seed = int(time.time() * 1000) % 100000

        self.norm_mean = torch.mean(self.norm_matrix, dim=1).cpu().detach().numpy()
        if not self.balance:
            top_examples = self.train_indx[np.argsort(self.norm_mean)][::-1][:self.coreset_size]
        else:
            top_examples = np.array([], dtype=np.int64)
            for c in range(self.num_classes):
                c_indx = self.train_indx[self.dst_train.targets == c]
                budget = round(self.fraction * len(c_indx))
                top_examples = np.append(top_examples, c_indx[np.argsort(self.norm_mean[c_indx])[::-1][:budget]])



        self.selection_result = {"indices": top_examples, "scores": self.norm_mean}
        if self.args.after_analyses:
            analyses_dict = OrderedDict()
            analyses_dict['checkpoint_name'] = self.args.checkpoint_name
            # eigen_dict = self.save_feature_and_classifier()

            '''difference for whole instances'''
            loss_difference, gradient_difference_norm, hessian_difference_norm, hessian_max_eigen = self.cal_loss_gradient_eigen()

            analyses_dict['global_loss_diff'] = loss_difference
            analyses_dict['global_grad_l2_norm'] = gradient_difference_norm
            analyses_dict['global_hess_l1_norm'] = hessian_difference_norm
            analyses_dict['global_hess_max_eigen'] = hessian_max_eigen
            # analyses_dict['global_hess_exact_max_eigen'] = eigen_dict[0]

            '''differences for class-wise instances'''
            for c in range(self.num_classes):
                c_indx = self.train_indx[self.dst_train.targets == c]
                loss_difference, gradient_difference_norm, hessian_difference_norm, hessian_max_eigen = self.cal_loss_gradient_eigen(c_indx)
                analyses_dict['global_loss_diff_'+str(c)] = loss_difference
                analyses_dict['global_grad_l2_norm_'+str(c)] = gradient_difference_norm
                analyses_dict['global_hess_l1_norm_'+str(c)] = hessian_difference_norm
                # analyses_dict['global_hess_max_eigen_'+str(c)] = hessian_max_eigen

            save_important_statistics(self.args, analyses_dict, 'analyses')



        return {"indices": top_examples, "scores": self.norm_mean}
