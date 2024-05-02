from .earlytrain import EarlyTrain
import torch
from .methods_utils import FacilityLocation, submodular_optimizer
import numpy as np
from .methods_utils.euclidean import euclidean_dist_pair_np
from ..nets.nets_utils import MyDataParallel
from utils import *
from backpack import backpack, extend
from backpack.extensions import BatchGrad, DiagHessian, BatchDiagHessian
from collections import OrderedDict
import os

class Craig(EarlyTrain):
    def __init__(self, dst_train, args, fraction=0.5, random_seed=None, epochs=200, specific_model=None,
                 balance=True, greedy="LazyGreedy", **kwargs):
        super().__init__(dst_train, args, fraction, random_seed, epochs, specific_model, **kwargs)

        if greedy not in submodular_optimizer.optimizer_choices:
            raise ModuleNotFoundError("Greedy optimizer not found.")
        self._greedy = greedy
        self.balance = balance

    def before_train(self):
        pass

    def after_loss(self, outputs, loss, targets, batch_inds, epoch):
        pass

    def before_epoch(self):
        pass

    def after_epoch(self):
        pass

    def before_run(self):
        pass

    def num_classes_mismatch(self):
        raise ValueError("num_classes of pretrain dataset does not match that of the training dataset.")

    def while_update(self, outputs, loss, targets, epoch, batch_idx, batch_size):
        if batch_idx % self.args.print_freq == 0:
            print('| Epoch [%3d/%3d] Iter[%3d/%3d]\t\tLoss: %.4f' % (
                epoch, self.epochs, batch_idx + 1, (self.n_pretrain_size // batch_size) + 1, loss.item()))

    def calc_gradient(self, index=None):
        self.model.eval()

        batch_loader = torch.utils.data.DataLoader(
            self.dst_train if index is None else torch.utils.data.Subset(self.dst_train, index),
            batch_size=self.args.selection_batch, num_workers=self.args.workers)
        sample_num = len(self.dst_val.targets) if index is None else len(index)
        self.embedding_dim = self.model.get_last_layer().in_features

        gradients = []

        for i, (input, targets) in enumerate(batch_loader):
            self.model_optimizer.zero_grad()
            outputs = self.model(input.to(self.args.device))
            loss = self.criterion(torch.nn.functional.softmax(outputs.requires_grad_(True), dim=1),
                                  targets.to(self.args.device)).sum()
            batch_num = targets.shape[0]
            with torch.no_grad():
                bias_parameters_grads = torch.autograd.grad(loss, outputs)[0]
                weight_parameters_grads = self.model.embedding_recorder.embedding.view(batch_num, 1,
                                                                                       self.embedding_dim).repeat(1,
                                                                                                                  self.args.num_classes,
                                                                                                                  1) * bias_parameters_grads.view(
                    batch_num, self.args.num_classes, 1).repeat(1, 1, self.embedding_dim)
                gradients.append(
                    torch.cat([bias_parameters_grads, weight_parameters_grads.flatten(1)], dim=1).cpu().numpy())

        gradients = np.concatenate(gradients, axis=0)

        self.model.train()
        return euclidean_dist_pair_np(gradients)

    def calc_weights(self, matrix, result):
        min_sample = np.argmax(matrix[result], axis=0)
        weights = np.ones(np.sum(result) if result.dtype == bool else len(result))
        for i in min_sample:
            weights[i] = weights[i] + 1
        return weights

    def finish_run(self):
        if isinstance(self.model, MyDataParallel):
            self.model = self.model.module

        self.model.no_grad = True
        with self.model.embedding_recorder:
            if self.balance:
                # Do selection by class
                selection_result = np.array([], dtype=np.int32)
                weights = np.array([])
                for c in range(self.args.num_classes):
                    class_index = np.arange(self.n_train)[self.dst_train.targets == c]
                    matrix = -1. * self.calc_gradient(class_index)
                    matrix -= np.min(matrix) - 1e-3
                    submod_function = FacilityLocation(index=class_index, similarity_matrix=matrix)
                    submod_optimizer = submodular_optimizer.__dict__[self._greedy](args=self.args, index=class_index,
                                                                                   budget=round(self.fraction * len(
                                                                                       class_index)))
                    class_result = submod_optimizer.select(gain_function=submod_function.calc_gain,
                                                           update_state=submod_function.update_state)
                    selection_result = np.append(selection_result, class_result)
                    weights = np.append(weights, self.calc_weights(matrix, np.isin(class_index, class_result)))
            else:
                matrix = np.zeros([self.n_train, self.n_train])
                all_index = np.arange(self.n_train)
                for c in range(self.args.num_classes):  # Sparse Matrix
                    class_index = np.arange(self.n_train)[self.dst_train.targets == c]
                    matrix[np.ix_(class_index, class_index)] = -1. * self.calc_gradient(class_index)
                    matrix[np.ix_(class_index, class_index)] -= np.min(matrix[np.ix_(class_index, class_index)]) - 1e-3
                submod_function = FacilityLocation(index=all_index, similarity_matrix=matrix)
                submod_optimizer = submodular_optimizer.__dict__[self._greedy](args=self.args, index=all_index,
                                                                               budget=self.coreset_size)
                selection_result = submod_optimizer.select(gain_function=submod_function.calc_gain_batch,
                                                           update_state=submod_function.update_state,
                                                           batch=self.args.selection_batch)
                weights = self.calc_weights(matrix, selection_result)
        self.model.no_grad = False
        return {"indices": selection_result, "weights": weights}

    def select(self, **kwargs):
        selection_result = self.run()
        self.selection_result = selection_result

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
        return selection_result
