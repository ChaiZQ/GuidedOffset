import _init_paths

import cv2
import time
import argparse
import logging
import pprint
import os
import sys
from config.config import config, update_config

os.environ['MXNET_CUDNN_AUTOTUNE_DEFAULT'] = '0'


def parse_args():
    parser = argparse.ArgumentParser(description='Train R-FCN network')
    # general
    parser.add_argument('--cfg', help='experiment configure file name', required=True, type=str)

    args, rest = parser.parse_known_args()
    # update config
    update_config(args.cfg)

    # training
    parser.add_argument('--frequent', help='frequency of logging', default=100, type=int)
    args = parser.parse_args()
    return args


args = parse_args()
curr_path = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(curr_path, '../external/mxnet', config.MXNET_VERSION))

import shutil
import numpy as np
import mxnet as mx

from symbols import *
from core import callback, metric
from core.loader import AnchorLoader
from core.module import MutableModule
from utils.create_logger import create_logger
from utils.load_data import load_gt_roidb, merge_roidb, filter_roidb
from utils.load_model import load_param
from utils.PrefetchingIter import PrefetchingIter
from utils.lr_scheduler import WarmupMultiFactorScheduler


def train_net(args, ctx, pretrained, epoch, prefix, begin_epoch, end_epoch, lr, lr_step):
    logger, final_output_path = create_logger(config.output_path, args.cfg, config.dataset.image_set)
    # @MyCode
    # smoothness_penalty_weight = float(config.mycfg.smoothness_penalty_weight)
    # smoothness_penalty_bias = float(config.mycfg.smoothness_penalty_bias)
    # ABCVar_penalty_weight = float(config.mycfg.ABCVar_penalty_weight)
    smoothness_roipool_weight = float(config.mycfg.smoothness_roipool_weight)

    prefix = os.path.join(final_output_path, prefix)

    # load symbol
    shutil.copy2(os.path.join(curr_path, 'symbols', config.symbol + '.py'), final_output_path)
    sym_instance = eval(config.symbol + '.' + config.symbol)()
    sym = sym_instance.get_symbol(config, is_train=True)
    feat_sym = sym.get_internals()['rpn_cls_score_output']

    # setup multi-gpu
    # batch_size = len(ctx)
    batch_size = 4
    input_batch_size = config.TRAIN.BATCH_IMAGES * batch_size

    # print config
    # pprint.pprint(config)
    # logger.info('training config:{}\n'.format(pprint.pformat(config)))

    # load dataset and prepare imdb for training
    image_sets = [iset for iset in config.dataset.image_set.split('+')]
    t = time.time()
    roidbs = [load_gt_roidb(config.dataset.dataset, image_set, config.dataset.root_path, config.dataset.dataset_path,
                            flip=config.TRAIN.FLIP)
              for image_set in image_sets]
    # print "data loading:%.2fs" % (time.time() - t)
    roidb = merge_roidb(roidbs)
    roidb = filter_roidb(roidb, config)
    # load training data
    train_data = AnchorLoader(feat_sym, roidb, config, batch_size=input_batch_size, shuffle=config.TRAIN.SHUFFLE,
                              ctx=ctx,
                              feat_stride=config.network.RPN_FEAT_STRIDE, anchor_scales=config.network.ANCHOR_SCALES,
                              anchor_ratios=config.network.ANCHOR_RATIOS, aspect_grouping=config.TRAIN.ASPECT_GROUPING)

    # infer max shape
    max_data_shape = [('data', (
        config.TRAIN.BATCH_IMAGES, 3, max([v[0] for v in config.SCALES]), max([v[1] for v in config.SCALES])))]
    max_data_shape, max_label_shape = train_data.infer_shape(max_data_shape)
    max_data_shape.append(('gt_boxes', (config.TRAIN.BATCH_IMAGES, 100, 5)))
    # print('providing maximum shape', max_data_shape, max_label_shape)

    data_shape_dict = dict(train_data.provide_data_single + train_data.provide_label_single)
    # pprint.pprint(data_shape_dict)
    sym_instance.infer_shape(data_shape_dict)
    # pprint.pprint(sym_instance.out_shape_dict)
    # load and initialize params
    if config.TRAIN.RESUME:
        print('continue training from ', begin_epoch)
        arg_params, aux_params = load_param(prefix, begin_epoch, convert=True)
    else:
        arg_params, aux_params = load_param(pretrained, epoch, convert=True)
        sym_instance.init_weight(config, arg_params, aux_params)
    fixed_param_prefix = config.network.FIXED_PARAMS

    # @MyCode
    # fixed_param_prefix.append('smoothness_penalty_weight')
    # fixed_param_prefix.append('smoothness_penalty_bias')
    # fixed_param_prefix.append('smoothness_penalty_kernel')
    # fixed_param_prefix.append('ABCVar_penalty_weight')
    # offset: [1,18,39,59]
    # kernel: should be [8,18,1,1]
    # out: [1,8,39,59]
    # err for (Px+Py)^2, it should be (Px^2+Py^2), but them may be same.
    # conv_kernel = mx.ndarray.array([[1, 1, 0, 0, 0, 0, 0, 0, -2, -2, 0, 0, 0, 0, 0, 0, 1, 1],
    #                                 [0, 0, 1, 1, 0, 0, 0, 0, -2, -2, 0, 0, 0, 0, 1, 1, 0, 0],
    #                                 [0, 0, 0, 0, 0, 0, 1, 1, -2, -2, 1, 1, 0, 0, 0, 0, 0, 0],
    #                                 [0, 0, 0, 0, 1, 1, 0, 0, -2, -2, 0, 0, 1, 1, 0, 0, 0, 0]])
    # arg_params['smoothness_penalty_kernel'] = conv_kernel.expand_dims(2).expand_dims(3)
    # arg_params['smoothness_penalty_weight'] = mx.ndarray.array([smoothness_penalty_weight])
    # arg_params['smoothness_penalty_bias'] = mx.ndarray.array([smoothness_penalty_bias])
    # arg_params['ABCVar_penalty_weight'] = mx.ndarray.array([ABCVar_penalty_weight])

    # offset: [300, 42, 7, 7] => [300*21,2,7,7]
    # kernel:                 => [8,2,3,3]
    # out: [300*21,8,5,5]
    roipool_kernel = mx.ndarray.array([
        [[[1, 0, 0],
          [0, -2, 0],
          [0, 0, 1]],
         [[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]]],
        [[[0, 1, 0],
          [0, -2, 0],
          [0, 1, 0]],
         [[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]]],
        [[[0, 0, 1],
          [0, -2, 0],
          [1, 0, 0]],
         [[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]]],
        [[[0, 0, 0],
          [1, -2, 1],
          [0, 0, 0]],
         [[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]]],
        [[[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]],
         [[1, 0, 0],
          [0, -2, 0],
          [0, 0, 1]]],
        [[[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]],
         [[0, 1, 0],
          [0, -2, 0],
          [0, 1, 0]]],
        [[[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]],
         [[0, 0, 1],
          [0, -2, 0],
          [1, 0, 0]]],
        [[[0, 0, 0],
          [0, 0, 0],
          [0, 0, 0]],
         [[0, 0, 0],
          [1, -2, 1],
          [0, 0, 0]]],
    ])
    arg_params['smoothness_roipool_kernel'] = roipool_kernel
    arg_params['smoothness_roipool_weight'] = mx.ndarray.array([smoothness_roipool_weight])
    fixed_param_prefix.append('smoothness_roipool_kernel')
    fixed_param_prefix.append('smoothness_roipool_weight')
    logger.info('@@@@smoothness_roipool_weight:' + str(smoothness_roipool_weight))
    pprint.pprint('@@@@smoothness_roipool_weight:' + str(smoothness_roipool_weight))
    logger.info('@@@@batch_size:' + str(batch_size))
    pprint.pprint('@@@@batch_size:' + str(batch_size))
    # logger.info('@@@@smoothness_penalty_weight:' + str(smoothness_penalty_weight))
    # pprint.pprint('@@@@smoothness_penalty_weight:' + str(smoothness_penalty_weight))
    # logger.info('@@@@smoothness_penalty_bias:' + str(smoothness_penalty_bias))
    # pprint.pprint('@@@@smoothness_penalty_bias:' + str(smoothness_penalty_bias))
    # logger.info('@@@@ABCVar_penalty_weight:' + str(ABCVar_penalty_weight))
    # pprint.pprint('@@@@ABCVar_penalty_weight:' + str(ABCVar_penalty_weight))
    logger.info('@@@@prefix:' + prefix)
    pprint.pprint('@@@@prefix:' + prefix)

    # check parameter shapes
    sym_instance.check_parameter_shapes(arg_params, aux_params, data_shape_dict)

    # create solver
    data_names = [k[0] for k in train_data.provide_data_single]
    label_names = [k[0] for k in train_data.provide_label_single]

    mod = MutableModule(sym, data_names=data_names, label_names=label_names,
                        logger=logger, context=ctx, max_data_shapes=[max_data_shape for _ in range(batch_size)],
                        max_label_shapes=[max_label_shape for _ in range(batch_size)],
                        fixed_param_prefix=fixed_param_prefix)

    if config.TRAIN.RESUME:
        mod._preload_opt_states = '%s-%04d.states' % (prefix, begin_epoch)

    # decide training params
    # metric
    rpn_eval_metric = metric.RPNAccMetric()
    rpn_cls_metric = metric.RPNLogLossMetric()
    rpn_bbox_metric = metric.RPNL1LossMetric()
    eval_metric = metric.RCNNAccMetric(config)
    cls_metric = metric.RCNNLogLossMetric(config)
    bbox_metric = metric.RCNNL1LossMetric(config)
    eval_metrics = mx.metric.CompositeEvalMetric()
    # @MyCode
    eval_metrics.add(metric.ROIPoolPenalty())
    # eval_metrics.add(metric.SmoothnessPenalty())
    # eval_metrics.add(metric.APenalty())
    # eval_metrics.add(metric.BPenalty())
    # eval_metrics.add(metric.CPenalty())
    # eval_metrics.add(metric.ABCVarPenalty())

    # rpn_eval_metric, rpn_cls_metric, rpn_bbox_metric, eval_metric, cls_metric, bbox_metric
    # for child_metric in [eval_metric, rpn_eval_metric, rpn_cls_metric, rpn_bbox_metric, cls_metric, bbox_metric]:
    for child_metric in [eval_metric]:
        eval_metrics.add(child_metric)
    # callback
    batch_end_callback = callback.Speedometer(train_data.batch_size, frequent=args.frequent)
    means = np.tile(np.array(config.TRAIN.BBOX_MEANS), 2 if config.CLASS_AGNOSTIC else config.dataset.NUM_CLASSES)
    stds = np.tile(np.array(config.TRAIN.BBOX_STDS), 2 if config.CLASS_AGNOSTIC else config.dataset.NUM_CLASSES)
    epoch_end_callback = [mx.callback.module_checkpoint(mod, prefix, period=1, save_optimizer_states=True),
                          callback.do_checkpoint(prefix, means, stds)]
    # decide learning rate
    base_lr = lr
    lr_factor = config.TRAIN.lr_factor
    lr_epoch = [float(epoch) for epoch in lr_step.split(',')]
    lr_epoch_diff = [epoch - begin_epoch for epoch in lr_epoch if epoch > begin_epoch]
    lr = base_lr * (lr_factor ** (len(lr_epoch) - len(lr_epoch_diff)))
    lr_iters = [int(epoch * len(roidb) / batch_size) for epoch in lr_epoch_diff]
    print('lr', lr, 'lr_epoch_diff', lr_epoch_diff, 'lr_iters', lr_iters)
    lr_scheduler = WarmupMultiFactorScheduler(lr_iters, lr_factor, config.TRAIN.warmup, config.TRAIN.warmup_lr,
                                              config.TRAIN.warmup_step)
    # optimizer
    optimizer_params = {'momentum'     : config.TRAIN.momentum,

                        'wd'           : config.TRAIN.wd,
                        'learning_rate': lr,
                        'lr_scheduler' : lr_scheduler,
                        'rescale_grad' : 1.0,
                        'clip_gradient': None}

    if not isinstance(train_data, PrefetchingIter):
        train_data = PrefetchingIter(train_data)

    mod.fit(train_data, eval_metric=eval_metrics, epoch_end_callback=epoch_end_callback,
            batch_end_callback=batch_end_callback, kvstore=config.default.kvstore,
            optimizer='sgd', optimizer_params=optimizer_params,
            arg_params=arg_params, aux_params=aux_params, begin_epoch=begin_epoch, num_epoch=end_epoch)


def main():
    print('Called with argument:', args)
    ctx = [mx.gpu(int(i)) for i in config.gpus.split(',')]
    print(ctx)
    train_net(args, ctx, config.network.pretrained, config.network.pretrained_epoch, config.TRAIN.model_prefix,
              config.TRAIN.begin_epoch, config.TRAIN.end_epoch, config.TRAIN.lr, config.TRAIN.lr_step)


if __name__ == '__main__':
    main()
