#!/usr/bin/env python
# -*- coding:utf-8 -*-
__author__ = 'fkb'
__email__ = 'fkb@zjuici.com'

import os
import random
from datetime import datetime

import torch
torch.cuda.empty_cache()
from torch.utils import data as torch_data
from transformers import AutoTokenizer
from tqdm import tqdm
import argparse
from config import Config
from src.utils import save, load, pipeline
from src.utils.eva import Evaluater
from torch.optim.lr_scheduler import LambdaLR
from tensorboardX import SummaryWriter
import time
import torch.nn.functional as F

#os.environ["CUDA_VISIBLE_DEVICES"] = "1"
# torch.cuda.set_device(1)

class ScheduledSampler():
    def __init__(self, phases):
        self.phases = phases
        self.scheduled_probs = [(i / (self.phases - 1) + 0.3) for i in range(self.phases)]

    def teacher_forcing(self, phase):
        """According to a certain probability to choose whether to execute teacher_forcing
        Args:
            phase (int): probability level  if phase = 0, 100% teacher_forcing ,phase = self.phases - 1, 0% teacher_forcing
        Returns:
            bool: teacher_forcing or not
        """
        sampling_prob = random.random()
        if phase < self.phases and sampling_prob >= self.scheduled_probs[phase]:
            return True
        else:
            return False


class Restorer(object):
    """docstring for Restorer"""

    def __init__(self, args):
        super(Restorer, self).__init__()
        self.config = Config()
        self.training = args.training
        self.pretrain = args.pretrain
        self.initialize()
        self.load_data()
        self.setup_model()

    def compute_kl_loss(self, p, q, pad_mask=None):
        p_loss = F.kl_div(F.log_softmax(p, dim=-1), F.softmax(q, dim=-1), reduction='none')
        q_loss = F.kl_div(F.log_softmax(q, dim=-1), F.softmax(p, dim=-1), reduction='none')

        # pad_mask is for seq-level tasks
        if pad_mask is not None:
            p_loss.masked_fill_(pad_mask, 0.)
            q_loss.masked_fill_(pad_mask, 0.)

        # You can choose whether to use function "sum" and "mean" depending on your task
        p_loss = p_loss.mean()  # p_loss.sum()
        q_loss = q_loss.mean()  # q_loss.sum()

        loss = (p_loss + q_loss) / 2
        return loss

    def initialize(self):
        # for reproducibility
        random.seed(self.config.random_seed)
        torch.manual_seed(self.config.random_seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        # verify devices which can be either cpu or gpu
        self.config.use_gpu = torch.cuda.is_available()
        self.config.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # training settings
        self.start_time = datetime.now()
        self.finished = False
        self.step, self.epoch = 0, 0
        self.valid_loss = float('inf')
        self.valid_log = ['Start Time: {}'.format(self.start_time)]
        self.ref_test_log = self.valid_log.copy()
        self.asr_test_log = self.valid_log.copy()
        # language model tokenizer
        if self.config.lan_model:
            self.tokenizer = AutoTokenizer.from_pretrained(self.config.lan_model)
            self.tokenizer.add_special_tokens({'bos_token': '<s>'})

    def load_data(self):
        for file in [self.config.TRAIN_FILE, self.config.VALID_FILE, self.config.REF_TEST_FILE, self.config.ASR_TEST_FILE]:
            if not os.path.exists(self.config.DATA_JSON.format(file)):
                raw_file = self.config.RAW_TXT.format(file)
                raw_data = pipeline.read_file(raw_file, self.config)
                raw_data = pipeline.parse_data(raw_data, self.tokenizer, self.config)
                save.save_json(self.config.DATA_JSON.format(file), raw_data)
        # train data loader
        train_dataset = pipeline.Dataset(
            file=self.config.TRAIN_FILE
            , config=self.config
            , is_train=True
            , training=self.training
        )
        self.trainset_generator = torch_data.DataLoader(
            train_dataset
            , batch_size=self.config.batch_size
            , collate_fn=self.collate_fn
            , shuffle=self.config.shuffle
            , num_workers=self.config.num_workers
            , pin_memory=self.config.pin_memory
            , drop_last=self.config.drop_last
        )
        # valid data loader
        valid_dataset = pipeline.Dataset(
            file=self.config.VALID_FILE
            , config=self.config
            , training=self.training
        )
        self.validset_generator = torch_data.DataLoader(
            valid_dataset
            , batch_size= 8
            , collate_fn=self.collate_fn
            , shuffle=False
            , num_workers=self.config.num_workers
            , pin_memory=self.config.pin_memory
            , drop_last=False)
        # ref test data loader
        ref_test_dataset = pipeline.Dataset(
            file=self.config.REF_TEST_FILE
            , config=self.config
            , training=self.training
        )
        self.ref_testset_generator = torch_data.DataLoader(
            ref_test_dataset
            , batch_size= 8
            , collate_fn=self.collate_fn
            , shuffle=False
            , num_workers=self.config.num_workers
            , pin_memory=self.config.pin_memory
            , drop_last=False)
        # asr test data loader
        asr_test_dataset = pipeline.Dataset(
            file=self.config.ASR_TEST_FILE
            , config=self.config
            , training=self.training)
        self.asr_testset_generator = torch_data.DataLoader(
            asr_test_dataset
            , batch_size=self.config.batch_size
            , collate_fn=self.collate_fn
            , shuffle=False
            , num_workers=self.config.num_workers
            , pin_memory=self.config.pin_memory
            , drop_last=False)
        # update config
        self.config.train_size = len(train_dataset)
        self.config.train_batch = len(self.trainset_generator)
        self.config.valid_size = len(valid_dataset)
        self.config.valid_batch = len(self.validset_generator)
        self.config.ref_test_size = len(ref_test_dataset)
        self.config.ref_test_batch = len(self.ref_testset_generator)
        self.config.asr_test_size = len(asr_test_dataset)
        self.config.asr_test_batch = len(self.asr_testset_generator)

    def collate_fn(self, data):
        # a customized collate function used in the data loader 
        data.sort(key=len, reverse=True)
        raw_xs, raw_ys, raw_y_masks = zip(*data)
        if self.config.lan_model:
            xs, x_masks, ys, y_masks = [], [], [], []
            for i in range(len(raw_xs)):
                x = raw_xs[i]
                y = raw_ys[i]
                y_mask = raw_y_masks[i]
                # padding
                if len(x) < self.config.max_seq_len:
                    diff_len = self.config.max_seq_len - len(x)
                    x += [self.config.PAD_TOKEN for _ in range(diff_len)]
                    y += [self.config.NORMAL_TOKEN for _ in range(diff_len)]
                    y_mask += [0 for _ in range(diff_len)]
                x_mask = [0 if token == self.config.PAD_TOKEN else 1 for token in x]
                x = self.tokenizer.convert_tokens_to_ids(x)
                y = pipeline.translate(y, self.config.label2idx_dict)
                xs.append(x)
                x_masks.append(x_mask)
                ys.append(y)
                y_masks.append(y_mask)

        return (raw_xs, raw_ys, raw_y_masks), (xs, x_masks, ys, y_masks)

    def load_check_point(self):
        checkpoint_to_load = torch.load(self.config.SAVE_POINT, map_location=self.config.device)
        self.step = checkpoint_to_load['step']
        self.epoch = checkpoint_to_load['epoch']
        model_state_dict = checkpoint_to_load['model']
        self.model.load_state_dict(model_state_dict)
        self.opt.load_state_dict(checkpoint_to_load['optimizer'])

    def setup_model(self):
        # loss function
        self.criterion = torch.nn.CrossEntropyLoss(reduction='none')
        # model
        self.model = pipeline.pick_model(self.config)
        if self.config.lan_model:
            if self.config.model_name == "parallelendecoder":
                decode_lan_model_param_names = ["decode_layer." + n for n, _ in self.model.decode_layer.named_parameters()]
                encode_lan_model_param_names = ["encode_layer." + n for n, _ in self.model.encode_layer.named_parameters()]

                decode_lan_model_params = list(map(lambda x: x[1], list(filter(lambda kv: kv[0]
                        in decode_lan_model_param_names, self.model.named_parameters()))))
                encode_lan_model_params = list(map(lambda x: x[1], list(filter(lambda kv: kv[0]
                        in encode_lan_model_param_names, self.model.named_parameters()))))
                head_model_params = list(map(lambda x: x[1], list(filter(lambda kv: kv[0]
                        not in (decode_lan_model_param_names + encode_lan_model_param_names)
                        , self.model.named_parameters()))))

                # set Adam optimizer
                self.opt = torch.optim.Adam([
                    {'params': decode_lan_model_params, 'lr': self.config.lan_learning_rate}
                    , {'params': encode_lan_model_params, 'lr': self.config.lan_learning_rate}
                    , {'params': head_model_params, 'lr': self.config.learning_rate}
                ])
            else:
                lan_model_param_names = [n for n, _ in self.model.named_parameters()]

                lan_model_params = list(map(lambda x: x[1], list(filter(lambda kv: kv[0]
                                in lan_model_param_names, self.model.named_parameters()))))
                head_model_params = list(map(lambda x: x[1], list(filter(lambda kv: kv[0]
                                not in lan_model_param_names, self.model.named_parameters()))))

                # set Adam optimizer
                self.opt = torch.optim.Adam([
                     {'params': lan_model_params, 'lr': self.config.lan_learning_rate}
                     , {'params': head_model_params, 'lr': self.config.learning_rate}
                 ])

        # learning rate
        self.scheduler = LambdaLR(self.opt, lr_lambda=lambda epoch: 0.95 ** epoch)
        self.writer = SummaryWriter(log_dir='./logs/' + time.strftime("%Y-%m-%d", time.localtime(time.time())))
        # restore training progress
        if self.config.load_check_point:
            self.load_check_point()
        # count trainable parameters
        self.config.num_parameters = pipeline.count_parameters(self.model)
        print(f'num_parameters: {self.config.num_parameters}')

    def train(self):
        general_info = pipeline.show_config(self.config, self.model)
        self.valid_log.append(general_info)
        self.ref_test_log.append(general_info)
        self.asr_test_log.append(general_info)
        while not self.finished:
            print('\nTraining...')
            train_loss, train_iteration = .0, 0
            all_xs, all_ys, all_y_masks, all_ys_ = [], [], [], []
            self.model.train()
            # training set data loader
            trainset_generator = tqdm(self.trainset_generator)
            for data in trainset_generator:
                raw_data, train_data = data
                train_data = (torch.LongTensor(i).to(self.config.device) for i in train_data)
                xs, x_masks, ys, y_masks = train_data
                ys_ = self.model(xs, x_masks, ys, y_masks)

                if self.config.use_rdrop:
                    ''' 添加 r-drop '''
                    ys_2 = self.model(xs, x_masks, ys, y_masks)
                    ce_loss = (self.criterion(ys_.view(-1, ys_.shape[-1]), ys.view(-1)) + self.criterion(ys_2.view(-1, ys_.shape[-1]), ys.view(-1))) / 2
                    kl_loss = self.compute_kl_loss(ys_, ys_2)
                    ''' 需要测试 2-5 '''
                    loss = ce_loss + 3 * kl_loss
                else:
                    loss = self.criterion(ys_.view(-1, ys_.shape[-1]), ys.view(-1))
    
                if self.config.mask_loss:
                    loss = (loss * y_masks.view(-1)).sum() / y_masks.sum()
                else:
                    loss = loss.mean()
                trainset_generator.set_description(
                    'Loss:{:.4f}'.format(loss.item()))

                loss.backward()
                if self.config.clipping_threshold:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.clipping_threshold)
                self.opt.step()
                self.opt.zero_grad()
                train_loss += loss.item()
                # postprocess
                xs, ys, y_masks, ys_ = pipeline.post_process(xs, x_masks, ys, y_masks, ys_, self.tokenizer, self.config)
                all_xs += xs
                all_ys += ys
                all_y_masks += y_masks
                all_ys_ += ys_

                train_iteration += 1
                self.step += 1
            #     break
            # break
            self.scheduler.step()
            print('lr: {}'.format(self.opt.state_dict()['param_groups'][0]['lr']))
            # check progress
            # evaluation
            loss = train_loss / train_iteration
            self.writer.add_scalar("loss/train", loss, self.epoch)

            eva_matrix = Evaluater(all_ys, all_y_masks, all_ys_, self.config)
            eva_msg = 'Train Epoch {} Total Step {} Loss:{:.4f} '.format(self.epoch, self.step, loss)
            eva_msg += eva_matrix.eva_msg
            print(eva_msg)
            # random sample to show
            self.validate()
            # reference test
            # self.ref_test()
            # asr output test
            # self.asr_test()
            # early stopping on the basis of validation result
            if self.valid_epoch >= self.config.valid_win_size:
                # if True:
                # update flag
                self.finished = True
                # reference test
                self.ref_test()
                # asr output test
                self.asr_test()
                # save log
                end_time = datetime.now()
                self.valid_log.append('\nEnd Time: {}'.format(end_time))
                self.valid_log.append('\nTotal Time: {}'.format(end_time - self.start_time))
                save.save_txt(self.config.LOG_POINT.format('val'), self.valid_log)
                self.ref_test_log += self.valid_log[-2:]
                self.asr_test_log += self.valid_log[-2:]
                save.save_txt(self.config.LOG_POINT.format('ref_test'), self.ref_test_log)
                save.save_txt(self.config.LOG_POINT.format('asr_test'), self.asr_test_log)
                # save val result
                valid_result = ['Src: {}\nTgt: {}\nPred: {}\n\n'.format(
                    x, y, y_) for x, y, y_ in zip(self.valid_src, self.valid_tgt, self.valid_pred)]
                save.save_txt(self.config.RESULT_POINT.format('val'), valid_result)
                # save ref test result
                ref_test_result = ['Src: {}\nTgt: {}\nPred: {}\n\n'.format(
                    x, y, y_) for x, y, y_ in zip(self.ref_test_src, self.ref_test_tgt, self.ref_test_pred)]
                save.save_txt(self.config.RESULT_POINT.format('ref_test'), ref_test_result)
                # save asr test result
                asr_test_result = ['Src: {}\nTgt: {}\nPred: {}\n\n'.format(
                    x, y, y_) for x, y, y_ in zip(self.asr_test_src, self.asr_test_tgt, self.asr_test_pred)]
                save.save_txt(self.config.RESULT_POINT.format('asr_test'), asr_test_result)

            self.epoch += 1
            self.valid_epoch += 1

    def validate(self):
        print('\nValidating...')
        valid_loss, valid_iteration = .0, 0
        all_xs, all_ys, all_y_masks, all_ys_ = [], [], [], []
        validset_generator = tqdm(self.validset_generator)
        self.model.eval()
        for data in validset_generator:
            raw_data, train_data = data
            train_data = (torch.LongTensor(i).to(self.config.device) for i in train_data)

            xs, x_masks, ys, y_masks = train_data
            ys_ = self.model(xs, x_masks)
            # print(xs[0])
            # print(x_masks[0])
            # print(ys[0])
            # print(torch.argmax(ys_, dim=-1).cpu().detach().numpy().tolist()[0])
            loss = self.criterion(ys_.view(-1, ys_.shape[-1]), ys.view(-1))
            if self.config.mask_loss:
                loss = (loss * y_masks.view(-1)).sum() / y_masks.sum()
            else:
                loss = loss.mean()
            valid_loss += loss.item()
            valid_iteration += 1
            # postprocess
            xs, ys, y_masks, ys_ = pipeline.post_process(xs, x_masks, ys, y_masks, ys_, self.tokenizer, self.config)
            all_xs += xs
            all_ys += ys
            all_y_masks += y_masks
            all_ys_ += ys_
            # break
        # evaluation
        loss = valid_loss / valid_iteration
        self.writer.add_scalar("loss/val", loss, self.epoch)
        eva_matrix = Evaluater(all_ys, all_y_masks, all_ys_, self.config)
        eva_msg = 'Val Epoch {} Total Step {}  Loss:{:.4f} '.format(self.epoch, self.step, loss)
        eva_msg += eva_matrix.eva_msg
        print(eva_msg)
        # record
        self.valid_log.append(eva_msg)
        # # random sample to show
        src, tar, pred = random.choice([(x, y, y_) for x, y, y_ in zip(all_xs, all_ys, all_ys_)])
        print(' src: {}\n tgt: {}\n pred: {}'.format(' '.join(src), ' '.join(tar), ' '.join(pred)))
        # early stopping
        if loss <= self.valid_loss:
            self.valid_epoch = 0
            self.valid_loss = loss
            # save model
            pipeline.save_model(
                self.step, self.epoch, self.model.state_dict, self.opt.state_dict, self.config.SAVE_POINT)
            # save val output
            self.valid_src = [' '.join(x) for x in all_xs]
            self.valid_tgt = [' '.join(y) for y in all_ys]
            self.valid_pred = [' '.join(y_) for y_ in all_ys_]

    def ref_test(self):
        print('\nReference Testing...')
        # restore model
        # model = pipeline.pick_model(self.config)
        # print('Model restored from {}.'.format(self.config.SAVE_POINT))
        # checkpoint_to_load = torch.load(self.config.SAVE_POINT, map_location=self.config.device)
        # model.load_state_dict(checkpoint_to_load['model'])
        # model.eval()

        # @@@
        self.model.eval()
        
        all_xs, all_ys, all_y_masks, all_ys_ = [], [], [], []
        ref_testset_generator = tqdm(self.ref_testset_generator)
        for data in ref_testset_generator:
            raw_data, train_data = data
            train_data = (torch.LongTensor(i).to(self.config.device) for i in train_data)

            xs, x_masks, ys, y_masks = train_data
            ys_ = self.model(xs, x_masks)
            # # postprocess
            xs, ys, y_masks, ys_ = pipeline.post_process(xs, x_masks, ys, y_masks, ys_, self.tokenizer, self.config)
            all_xs += xs
            all_ys += ys
            all_y_masks += y_masks
            all_ys_ += ys_
            # break
        eva_matrix = Evaluater(all_ys, all_y_masks, all_ys_, self.config)
        eva_msg = 'Test Epoch {} Total Step {} '.format(self.epoch, self.step)
        eva_msg += eva_matrix.eva_msg
        print(eva_msg)
        # record
        self.ref_test_log.append(eva_msg)
        # random sample to show
        # src, tar, pred = random.choice([(x, y, y_) for x, y, y_ in zip(all_xs, all_ys, all_ys_)])
        # print(' src: {}\n tgt: {}\n pred: {}'.format(' '.join(src), ' '.join(tar), ' '.join(pred)))
        # save test output
        self.ref_test_src = [' '.join(x) for x in all_xs]
        self.ref_test_tgt = [' '.join(y) for y in all_ys]
        self.ref_test_pred = [' '.join(y_) for y_ in all_ys_]

    def asr_test(self):
        print('\nASR Testing...')
        # restore model
        # model = pipeline.pick_model(self.config)
        # print('Model restored from {}.'.format(self.config.SAVE_POINT))
        # checkpoint_to_load = torch.load(self.config.SAVE_POINT, map_location=self.config.device)
        # model.load_state_dict(checkpoint_to_load['model'])
        # model.eval()

        # @@@
        self.model.eval()

        all_xs, all_ys, all_y_masks, all_ys_ = [], [], [], []
        asr_testset_generator = tqdm(self.asr_testset_generator)
        for data in asr_testset_generator:
            raw_data, train_data = data
            train_data = (torch.LongTensor(i).to(self.config.device) for i in train_data)

            xs, x_masks, ys, y_masks = train_data
            ys_ = self.model(xs, x_masks)
            # postprocess
            xs, ys, y_masks, ys_ = pipeline.post_process(xs, x_masks, ys, y_masks, ys_, self.tokenizer, self.config)
            all_xs += xs
            all_ys += ys
            all_y_masks += y_masks
            all_ys_ += ys_
            # break
        eva_matrix = Evaluater(all_ys, all_y_masks, all_ys_, self.config)
        eva_msg = 'Test Epoch {} Total Step {} '.format(self.epoch, self.step)
        eva_msg += eva_matrix.eva_msg
        print(eva_msg)
        # record
        self.asr_test_log.append(eva_msg)
        # random sample to show
        src, tar, pred = random.choice([(x, y, y_) for x, y, y_ in zip(all_xs, all_ys, all_ys_)])
        print(' src: {}\n tgt: {}\n pred: {}'.format(' '.join(src), ' '.join(tar), ' '.join(pred)))
        # save test output
        self.asr_test_src = [' '.join(x) for x in all_xs]
        self.asr_test_tgt = [' '.join(y) for y in all_ys]
        self.asr_test_pred = [' '.join(y_) for y_ in all_ys_]


def main(args):
    # initialize pipeline
    print('Initialize...')
    re = Restorer(args)
    # train
    re.train()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(allow_abbrev=True)
    parser.add_argument("--training", default=1, type=int)
    parser.add_argument("--learning_rate", default=3e-5, type=int)
    parser.add_argument("--pretrain", default=0, type=int)
    args = parser.parse_args()
    scheduled_sampler = ScheduledSampler(8)
    main(args)
