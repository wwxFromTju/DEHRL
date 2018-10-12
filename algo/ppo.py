import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import utils
import numpy as np

class PPO(object):

    def set_this_layer(self, this_layer):
        self.this_layer = this_layer
        self.init_actor_critic()

    def init_actor_critic(self):
        self.optimizer_actor_critic = optim.Adam(self.this_layer.actor_critic.parameters(), lr=self.this_layer.args.lr, eps=self.this_layer.args.eps)
        self.one = torch.FloatTensor([1]).cuda()
        self.mone = self.one * -1

    def set_upper_layer(self, upper_layer):
        '''this method will be called if we have a transition_model to generate reward bounty'''
        self.upper_layer = upper_layer
        if self.upper_layer.transition_model is not None:
            self.init_transition_model()

    def init_transition_model(self):
        '''build essential things for training transition_model'''
        self.optimizer_transition_model = optim.Adam(self.upper_layer.transition_model.parameters(), lr=1e-4, betas=(0.0, 0.9))
        self.NLLLoss = nn.NLLLoss(reduction='elementwise_mean')

    def get_grad_norm(self, inputs, outputs):

        gradients = torch.autograd.grad(
            outputs=outputs,
            inputs=inputs,
            grad_outputs=torch.ones(outputs.size()).cuda(),
            create_graph=True,
            retain_graph=True,
            only_inputs=True
        )[0]
        gradients = gradients.contiguous()
        gradients_fl = gradients.view(gradients.size()[0],-1)
        gradients_norm = gradients_fl.norm(2, dim=1) / ((gradients_fl.size()[1])**0.5)

        return gradients_norm

    def update(self, update_type):

        epoch_loss = {}

        '''train actor_critic'''
        if update_type in ['actor_critic','both']:

            '''compute advantages'''
            advantages = self.this_layer.rollouts.returns[:-1] - self.this_layer.rollouts.value_preds[:-1]
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-5)

            '''prepare epoch'''
            epoch = self.this_layer.args.actor_critic_epoch
            if self.this_layer.update_i in [0,1]:
                print('[H-{}] {}-th time train actor_critic, skip, since transition_model need to be trained first.'.format(
                    self.this_layer.hierarchy_id,
                    self.this_layer.update_i,
                ))
                epoch *= 0

            for e in range(epoch):

                data_generator = self.this_layer.rollouts.feed_forward_generator(
                    advantages = advantages,
                    mini_batch_size = self.this_layer.args.actor_critic_mini_batch_size,
                )

                for sample in data_generator:

                    self.optimizer_actor_critic.zero_grad()

                    observations_batch, input_actions_batch, states_batch, actions_batch, \
                       return_batch, masks_batch, old_action_log_probs_batch, \
                            adv_targ = sample

                    input_actions_index = input_actions_batch.nonzero()[:,1]

                    # Reshape to do in a single forward pass for all steps
                    values, action_log_probs, dist_entropy, _, dist_features = self.this_layer.actor_critic.evaluate_actions(
                        inputs       = observations_batch,
                        states       = states_batch,
                        masks        = masks_batch,
                        action       = actions_batch,
                        input_action = input_actions_batch,
                    )

                    ratio = torch.exp(action_log_probs - old_action_log_probs_batch)
                    surr1 = ratio * adv_targ
                    surr2 = torch.clamp(ratio, 1.0 - self.this_layer.args.clip_param,
                                               1.0 + self.this_layer.args.clip_param) * adv_targ
                    action_loss = -torch.min(surr1, surr2)
                    epoch_loss['action_{}'.format(input_actions_index[0])] = action_loss[0,0].item()
                    action_loss = action_loss.mean()

                    value_loss = (return_batch-values).pow(2) * self.this_layer.args.value_loss_coef
                    epoch_loss['value_{}'.format(input_actions_index[0])] = value_loss[0,0].item()
                    value_loss = value_loss.mean()

                    dist_entropy = dist_entropy * self.this_layer.args.entropy_coef
                    epoch_loss['dist_entropy_{}'.format(input_actions_index[0])] = dist_entropy[0].item()
                    dist_entropy = dist_entropy.mean()

                    final_loss = value_loss + action_loss - dist_entropy

                    final_loss.backward()

                    nn.utils.clip_grad_norm_(self.this_layer.actor_critic.parameters(),
                                             self.this_layer.args.max_grad_norm)

                    self.optimizer_actor_critic.step()

        '''train transition_model'''
        if update_type in ['transition_model','both']:

            '''prepare epoch'''
            epoch = self.this_layer.args.transition_model_epoch
            if self.this_layer.update_i in [0,1]:
                print('[H-{}] {}-th time train transition_model, train more epoch'.format(
                    self.this_layer.hierarchy_id,
                    self.this_layer.update_i,
                ))
                if not self.this_layer.checkpoint_loaded:
                    epoch = 800

            for e in range(epoch):

                data_generator = self.upper_layer.rollouts.transition_model_feed_forward_generator(
                    mini_batch_size = int(self.this_layer.args.transition_model_mini_batch_size[self.this_layer.hierarchy_id]),
                    recent_steps = int(self.this_layer.rollouts.num_steps/self.this_layer.hierarchy_interval)-1,
                    recent_at = self.upper_layer.step_i,
                )

                for sample in data_generator:

                    observations_batch, next_observations_batch, action_onehot_batch, reward_bounty_raw_batch = sample

                    self.optimizer_transition_model.zero_grad()

                    if self.this_layer.args.encourage_ac_connection in ['transition_model','both']:
                        action_onehot_batch = torch.autograd.Variable(action_onehot_batch, requires_grad=True)

                    '''forward'''
                    self.upper_layer.transition_model.train()

                    if not self.this_layer.args.mutual_information:
                        predicted_next_observations_batch, reward_bounty = self.upper_layer.transition_model(
                            inputs = observations_batch,
                            input_action = action_onehot_batch,
                        )
                        '''compute mse loss'''
                        loss_transition = F.mse_loss(
                            input = predicted_next_observations_batch,
                            target = (next_observations_batch-observations_batch[:,-1:]),
                            reduction='elementwise_mean',
                        )/255.0

                        if self.this_layer.args.inverse_mask:

                            action_lable_batch = action_onehot_batch.nonzero()[:,1]

                            '''compute loss_action'''
                            predicted_action_log_probs, loss_ent, predicted_action_log_probs_each = self.upper_layer.transition_model.inverse_mask_model(
                                last_states  = observations_batch[:,-1:],
                                now_states   = next_observations_batch,
                            )
                            loss_action = self.NLLLoss(predicted_action_log_probs, action_lable_batch)

                            '''compute loss_action_each'''
                            action_lable_batch_each = action_lable_batch.unsqueeze(1).expand(-1,predicted_action_log_probs_each.size()[1]).contiguous()
                            loss_action_each = self.NLLLoss(
                                predicted_action_log_probs_each.view(predicted_action_log_probs_each.size()[0] * predicted_action_log_probs_each.size()[1],predicted_action_log_probs_each.size()[2]),
                                action_lable_batch_each        .view(action_lable_batch_each        .size()[0] * action_lable_batch_each        .size()[1]                                          ),
                            ) * action_lable_batch_each.size()[1]

                            '''compute loss_inverse_mask_model'''
                            loss_inverse_mask_model = loss_action + loss_action_each + 0.001*loss_ent

                            loss_transition_final = loss_transition + loss_inverse_mask_model

                        else:
                            loss_transition_final = loss_transition

                    else:
                        predicted_action_log_probs, reward_bounty = self.upper_layer.transition_model(
                            inputs = next_observations_batch,
                        )
                        '''compute nll loss'''
                        loss_mutual_information = self.NLLLoss(predicted_action_log_probs, action_onehot_batch.nonzero()[:,1])

                    if self.this_layer.update_i not in [0]:
                        '''for the first epoch, reward bounty is not accurate'''
                        loss_reward_bounty = F.mse_loss(
                            input = reward_bounty,
                            target = reward_bounty_raw_batch,
                            reduction='elementwise_mean',
                        )

                    if not self.this_layer.args.mutual_information:
                        if self.this_layer.update_i not in [0]:
                            loss_final = loss_transition_final + loss_reward_bounty
                        else:
                            loss_final = loss_transition_final
                    else:
                        if self.this_layer.update_i not in [0]:
                            loss_final = loss_mutual_information + loss_reward_bounty
                        else:
                            loss_final = loss_mutual_information

                    '''backward'''
                    loss_final.backward(
                        retain_graph=self.this_layer.args.inverse_mask,
                    )

                    self.optimizer_transition_model.step()

                if self.this_layer.update_i in [0,1]:
                    print_str = ''
                    print_str += '[H-{}] {}-th time train transition_model, epoch {}, lf {}'.format(
                        self.this_layer.hierarchy_id,
                        self.this_layer.update_i,
                        e,
                        loss_final.item(),
                    )
                    if self.this_layer.update_i not in [0]:
                        print_str += ', lrb {}'.format(
                            loss_reward_bounty.item(),
                        )
                    if not self.this_layer.args.mutual_information:
                        print_str += ', lt {}'.format(
                            loss_transition.item(),
                        )
                        if self.this_layer.args.inverse_mask:
                            print_str += ', limm {}'.format(
                                loss_inverse_mask_model.item(),
                            )
                    else:
                        print_str += ', lmi {}'.format(
                            loss_mutual_information.item(),
                        )
                    print(print_str)

            if not self.this_layer.args.mutual_information:
                epoch_loss['loss_transition'] = loss_transition.item()
                if self.this_layer.args.inverse_mask:
                    epoch_loss['loss_inverse_mask_model'] = loss_inverse_mask_model.item()
            else:
                epoch_loss['loss_mutual_information'] = loss_mutual_information.item()
            if self.this_layer.update_i not in [0]:
                epoch_loss['loss_reward_bounty'] = loss_reward_bounty.item()

        return epoch_loss
