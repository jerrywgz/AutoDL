#  Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.
#
# Based on:
# --------------------------------------------------------
# DARTS
# Copyright (c) 2018, Hanxiao Liu.
# Licensed under the Apache License, Version 2.0;
# --------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from learning_rate import cosine_decay
import numpy as np
import argparse
from model import NetworkCIFAR as Network
import reader_cifar as reader
import sys
import os
import time
import logging
import genotypes
import paddle.fluid as fluid
import shutil
import utils
import math

parser = argparse.ArgumentParser("cifar")
# yapf: disable
parser.add_argument('--data', type=str, default='./dataset/cifar/cifar-10-batches-py/', help='location of the data corpus')
parser.add_argument('--batch_size', type=int, default=96, help='batch size')
parser.add_argument('--pretrained_model', type=str, default=None, help='pretrained model to load')
parser.add_argument('--model_id', type=int, help='model id')
parser.add_argument('--learning_rate', type=float, default=0.025, help='init learning rate')
parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
parser.add_argument('--weight_decay', type=float, default=3e-4, help='weight decay')
parser.add_argument('--report_freq', type=float, default=50, help='report frequency')
parser.add_argument('--epochs', type=int, default=600, help='num of training epochs')
parser.add_argument('--init_channels', type=int, default=36, help='num of init channels')
parser.add_argument('--layers', type=int, default=20, help='total number of layers')
parser.add_argument('--save_model_path', type=str, default='saved_models', help='path to save the model')
parser.add_argument('--auxiliary', action='store_true', default=False, help='use auxiliary tower')
parser.add_argument('--auxiliary_weight', type=float, default=0.4, help='weight for auxiliary loss')
parser.add_argument('--cutout', action='store_true', default=False, help='use cutout')
parser.add_argument('--cutout_length', type=int, default=16, help='cutout length')
parser.add_argument('--drop_path_prob', type=float, default=0.2, help='drop path probability')
parser.add_argument('--arch', type=str, default='DARTS', help='which architecture to use')
parser.add_argument('--grad_clip', type=float, default=5, help='gradient clipping')
parser.add_argument('--lr_exp_decay', action='store_true', default=False, help='use exponential_decay learning_rate')
parser.add_argument('--mix_alpha', type=float, default=0.5, help='mixup alpha')
parser.add_argument('--lrc_loss_lambda', default=0, type=float, help='lrc_loss_lambda')
# yapf: enable

args = parser.parse_args()

CIFAR_CLASSES = 10
dataset_train_size = 50000.
image_size = 32
genotypes.DARTS = genotypes.MY_DARTS_list[args.model_id]

def main():
    image_shape = [3, image_size, image_size]
    devices = os.getenv("CUDA_VISIBLE_DEVICES") or ""
    devices_num = len(devices.split(","))
    logging.info("args = %s", args)
    genotype = eval("genotypes.%s" % args.arch)
    model = Network(args.init_channels, CIFAR_CLASSES, args.layers,
                    args.auxiliary, genotype)
    
    steps_one_epoch = math.ceil(dataset_train_size / (devices_num * args.batch_size))
    train(model, args, image_shape, steps_one_epoch)


def build_program(main_prog, startup_prog, args, is_train, model, im_shape,
                  steps_one_epoch):
    out = []
    with fluid.program_guard(main_prog, startup_prog):
        py_reader = model.build_input(im_shape, is_train)
        if is_train:
            with fluid.unique_name.guard():
                loss = model.train_model(py_reader, args.init_channels,
                                         args.auxiliary, args.auxiliary_weight,
                                         args.lrc_loss_lambda)
                optimizer = fluid.optimizer.Momentum(
                    learning_rate=cosine_decay(args.learning_rate, args.epochs,
                                               steps_one_epoch),
                    regularization=fluid.regularizer.L2Decay(args.weight_decay),
                    momentum=args.momentum)
                optimizer.minimize(loss)
                out = [py_reader, loss]
        else:
            with fluid.unique_name.guard():
                prob, acc_1, acc_5 = model.test_model(py_reader,
                                                      args.init_channels)
                out = [py_reader, prob, acc_1, acc_5]
    return out


def train(model, args, im_shape, steps_one_epoch):
    startup_prog = fluid.Program()
    train_prog = fluid.Program()
    test_prog = fluid.Program()

    train_py_reader, loss_train = build_program(
        train_prog, startup_prog, args, True, model, im_shape, steps_one_epoch)

    test_py_reader, prob, acc_1, acc_5 = build_program(
        test_prog, startup_prog, args, False, model, im_shape, steps_one_epoch)

    test_prog = test_prog.clone(for_test=True)

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    exe.run(startup_prog)

    if args.pretrained_model:

        def if_exist(var):
            return os.path.exists(os.path.join(args.pretrained_model, var.name))

        fluid.io.load_vars(
            exe,
            args.pretrained_model,
            main_program=train_prog,
            predicate=if_exist)

    #if args.pretrained_model:

    #    def if_exist(var):
    #        return os.path.exists(os.path.join(args.pretrained_model, var.name))

    #    fluid.io.load_vars(exe, args.pretrained_model, main_program=train_prog, predicate=if_exist)

    exec_strategy = fluid.ExecutionStrategy()
    exec_strategy.num_threads = 1
    build_strategy = fluid.BuildStrategy()
    build_strategy.memory_optimize = False
    build_strategy.enable_inplace = True

    compile_program = fluid.compiler.CompiledProgram(
        train_prog).with_data_parallel(
            loss_name=loss_train.name,
            build_strategy=build_strategy,
            exec_strategy=exec_strategy)

    train_reader = reader.train10(args)
    test_reader = reader.test10(args)
    train_py_reader.decorate_paddle_reader(train_reader)
    test_py_reader.decorate_paddle_reader(test_reader)

    fluid.clip.set_gradient_clip(
        fluid.clip.GradientClipByGlobalNorm(args.grad_clip), program=train_prog)
    train_fetch_list = [loss_train]

    def save_model(postfix, main_prog):
        model_path = os.path.join(args.save_model_path, postfix)
        if os.path.isdir(model_path):
            shutil.rmtree(model_path)
        fluid.io.save_persistables(exe, model_path, main_program=main_prog)

    def test(epoch_id):
        test_fetch_list = [prob, acc_1, acc_5]
        top1 = utils.AvgrageMeter()
        top5 = utils.AvgrageMeter()
        test_py_reader.start()
        test_start_time = time.time()
        step_id = 0
        try:
            while True:
                prev_test_start_time = test_start_time
                test_start_time = time.time()
                prob_v, acc_1_v, acc_5_v = exe.run(test_prog,
                                                   fetch_list=test_fetch_list)
                top1.update(np.array(acc_1_v), np.array(prob_v).shape[0])
                top5.update(np.array(acc_5_v), np.array(prob_v).shape[0])
                if step_id % args.report_freq == 0:
                    print("Epoch {}, Step {}, acc_1 {}, acc_5 {}, time {}".
                          format(epoch_id, step_id,
                                 np.array(acc_1_v),
                                 np.array(acc_5_v), test_start_time -
                                 prev_test_start_time))
                step_id += 1
        except fluid.core.EOFException:
            test_py_reader.reset()
        print("Epoch {0}, top1 {1}, top5 {2}".format(epoch_id, top1.avg,
                                                     top5.avg))

    epoch_start_time = time.time()
    for epoch_id in range(args.epochs):
        model.drop_path_prob = args.drop_path_prob * epoch_id / args.epochs
        train_py_reader.start()
        epoch_end_time = time.time()
        if epoch_id > 0:
            print("Epoch {}, total time {}".format(epoch_id - 1, epoch_end_time
                                                   - epoch_start_time))
        epoch_start_time = epoch_end_time
        epoch_end_time
        start_time = time.time()
        step_id = 0
        try:
            while True:
                prev_start_time = start_time
                start_time = time.time()
                loss_v, = exe.run(
                    compile_program,
                    fetch_list=[v.name for v in train_fetch_list])
                print("Epoch {}, Step {}, loss {}, time {}".format(epoch_id, step_id, \
                        np.array(loss_v).mean(), start_time-prev_start_time))
                step_id += 1
                sys.stdout.flush()
        except fluid.core.EOFException:
            train_py_reader.reset()
        if epoch_id % 50 == 0:
            save_model(str(epoch_id), train_prog)
        if epoch_id == args.epochs - 1:
            save_model('final', train_prog)
        test(epoch_id)


if __name__ == '__main__':
    main()
