# Copyright 2020 Google Research. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Model function definition, including both architecture and loss."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import re
from absl import logging
import numpy as np
import tensorflow.compat.v1 as tf

import coco_metric
import efficientdet_arch
import hparams_config
import iou_utils
import retinanet_arch
import utils
from keras import anchors
from keras import postprocess

_DEFAULT_BATCH_SIZE = 64


def update_learning_rate_schedule_parameters(params):
  """Updates params that are related to the learning rate schedule."""
  # params['batch_size'] is per-shard within model_fn if strategy=tpu.
  batch_size = (
      params['batch_size'] * params['num_shards']
      if params['strategy'] == 'tpu' else params['batch_size'])
  # Learning rate is proportional to the batch size
  params['adjusted_learning_rate'] = (
      params['learning_rate'] * batch_size / _DEFAULT_BATCH_SIZE)
  steps_per_epoch = params['num_examples_per_epoch'] / batch_size
  params['lr_warmup_step'] = int(params['lr_warmup_epoch'] * steps_per_epoch)
  params['first_lr_drop_step'] = int(params['first_lr_drop_epoch'] *
                                     steps_per_epoch)
  params['second_lr_drop_step'] = int(params['second_lr_drop_epoch'] *
                                      steps_per_epoch)
  params['total_steps'] = int(params['num_epochs'] * steps_per_epoch)


def stepwise_lr_schedule(adjusted_learning_rate, lr_warmup_init, lr_warmup_step,
                         first_lr_drop_step, second_lr_drop_step, global_step):
  """Handles linear scaling rule, gradual warmup, and LR decay."""
  # lr_warmup_init is the starting learning rate; the learning rate is linearly
  # scaled up to the full learning rate after `lr_warmup_step` before decaying.
  logging.info('LR schedule method: stepwise')
  linear_warmup = (
      lr_warmup_init +
      (tf.cast(global_step, dtype=tf.float32) / lr_warmup_step *
       (adjusted_learning_rate - lr_warmup_init)))
  learning_rate = tf.where(global_step < lr_warmup_step, linear_warmup,
                           adjusted_learning_rate)
  lr_schedule = [[1.0, lr_warmup_step], [0.1, first_lr_drop_step],
                 [0.01, second_lr_drop_step]]
  for mult, start_global_step in lr_schedule:
    learning_rate = tf.where(global_step < start_global_step, learning_rate,
                             adjusted_learning_rate * mult)
  return learning_rate


def cosine_lr_schedule_tf2(adjusted_lr, lr_warmup_init, lr_warmup_step,
                           total_steps, step):
  """TF2 friendly cosine learning rate schedule."""
  logging.info('LR schedule method: cosine')

  def warmup_lr(step):
    return lr_warmup_init + (adjusted_lr - lr_warmup_init) * (
        tf.cast(step, tf.float32) / tf.cast(lr_warmup_step, tf.float32))

  def cosine_lr(step):
    decay_steps = tf.cast(total_steps - lr_warmup_step, tf.float32)
    step = tf.cast(step - lr_warmup_step, tf.float32)
    cosine_decay = 0.5 * (1 + tf.cos(np.pi * step / decay_steps))
    alpha = 0.0
    decayed = (1 - alpha) * cosine_decay + alpha
    return adjusted_lr * tf.cast(decayed, tf.float32)

  return tf.cond(step <= lr_warmup_step, lambda: warmup_lr(step),
                 lambda: cosine_lr(step))


def cosine_lr_schedule(adjusted_lr, lr_warmup_init, lr_warmup_step, total_steps,
                       step):
  logging.info('LR schedule method: cosine')
  linear_warmup = (
      lr_warmup_init + (tf.cast(step, dtype=tf.float32) / lr_warmup_step *
                        (adjusted_lr - lr_warmup_init)))
  cosine_lr = 0.5 * adjusted_lr * (
      1 + tf.cos(np.pi * tf.cast(step, tf.float32) / total_steps))
  return tf.where(step < lr_warmup_step, linear_warmup, cosine_lr)


def polynomial_lr_schedule(adjusted_lr, lr_warmup_init, lr_warmup_step, power,
                           total_steps, step):
  logging.info('LR schedule method: polynomial')
  linear_warmup = (
      lr_warmup_init + (tf.cast(step, dtype=tf.float32) / lr_warmup_step *
                        (adjusted_lr - lr_warmup_init)))
  polynomial_lr = adjusted_lr * tf.pow(
      1 - (tf.cast(step, tf.float32) / total_steps), power)
  return tf.where(step < lr_warmup_step, linear_warmup, polynomial_lr)


def learning_rate_schedule(params, global_step):
  """Learning rate schedule based on global step."""
  lr_decay_method = params['lr_decay_method']
  if lr_decay_method == 'stepwise':
    return stepwise_lr_schedule(params['adjusted_learning_rate'],
                                params['lr_warmup_init'],
                                params['lr_warmup_step'],
                                params['first_lr_drop_step'],
                                params['second_lr_drop_step'], global_step)

  if lr_decay_method == 'cosine':
    return cosine_lr_schedule(params['adjusted_learning_rate'],
                              params['lr_warmup_init'],
                              params['lr_warmup_step'], params['total_steps'],
                              global_step)

  if lr_decay_method == 'polynomial':
    return polynomial_lr_schedule(params['adjusted_learning_rate'],
                                  params['lr_warmup_init'],
                                  params['lr_warmup_step'],
                                  params['poly_lr_power'],
                                  params['total_steps'], global_step)

  if lr_decay_method == 'constant':
    return params['adjusted_learning_rate']

  raise ValueError('unknown lr_decay_method: {}'.format(lr_decay_method))


def focal_loss(y_pred, y_true, alpha, gamma, normalizer, label_smoothing=0.0):
  """Compute the focal loss between `logits` and the golden `target` values.

  Focal loss = -(1-pt)^gamma * log(pt)
  where pt is the probability of being classified to the true class.

  Args:
    y_pred: A float32 tensor of size [batch, height_in, width_in,
      num_predictions].
    y_true: A float32 tensor of size [batch, height_in, width_in,
      num_predictions].
    alpha: A float32 scalar multiplying alpha to the loss from positive examples
      and (1-alpha) to the loss from negative examples.
    gamma: A float32 scalar modulating loss from hard and easy examples.
    normalizer: Divide loss by this value.
    label_smoothing: Float in [0, 1]. If > `0` then smooth the labels.

  Returns:
    loss: A float32 scalar representing normalized total loss.
  """
  with tf.name_scope('focal_loss'):
    alpha = tf.convert_to_tensor(alpha, dtype=y_pred.dtype)
    gamma = tf.convert_to_tensor(gamma, dtype=y_pred.dtype)

    # compute focal loss multipliers before label smoothing, such that it will
    # not blow up the loss.
    pred_prob = tf.sigmoid(y_pred)
    p_t = (y_true * pred_prob) + ((1 - y_true) * (1 - pred_prob))
    alpha_factor = y_true * alpha + (1 - y_true) * (1 - alpha)
    modulating_factor = (1.0 - p_t) ** gamma

    # apply label smoothing for cross_entropy for each entry.
    y_true = y_true * (1.0 - label_smoothing) + 0.5 * label_smoothing
    ce = tf.nn.sigmoid_cross_entropy_with_logits(labels=y_true, logits=y_pred)

    # compute the final loss and return
    return alpha_factor * modulating_factor * ce / normalizer


def _box_loss(box_outputs, box_targets, num_positives, delta=0.1):
  """Computes box regression loss."""
  # delta is typically around the mean value of regression target.
  # for instances, the regression targets of 512x512 input with 6 anchors on
  # P3-P7 pyramid is about [0.1, 0.1, 0.2, 0.2].
  normalizer = num_positives * 4.0
  mask = tf.not_equal(box_targets, 0.0)
  box_loss = tf.losses.huber_loss(
      box_targets,
      box_outputs,
      weights=mask,
      delta=delta,
      reduction=tf.losses.Reduction.SUM)
  box_loss /= normalizer
  return box_loss


def _box_iou_loss(box_outputs, box_targets, num_positives, iou_loss_type):
  """Computes box iou loss."""
  normalizer = num_positives * 4.0
  box_iou_loss = iou_utils.iou_loss(box_outputs, box_targets, iou_loss_type)
  box_iou_loss = tf.reduce_sum(box_iou_loss) / normalizer
  return box_iou_loss


def detection_loss(cls_outputs, box_outputs, labels, params):
  """Computes total detection loss.

  Computes total detection loss including box and class loss from all levels.
  Args:
    cls_outputs: an OrderDict with keys representing levels and values
      representing logits in [batch_size, height, width, num_anchors].
    box_outputs: an OrderDict with keys representing levels and values
      representing box regression targets in [batch_size, height, width,
      num_anchors * 4].
    labels: the dictionary that returned from dataloader that includes
      groundtruth targets.
    params: the dictionary including training parameters specified in
      default_haprams function in this file.

  Returns:
    total_loss: an integer tensor representing total loss reducing from
      class and box losses from all levels.
    cls_loss: an integer tensor representing total class loss.
    box_loss: an integer tensor representing total box regression loss.
    box_iou_loss: an integer tensor representing total box iou loss.
  """
  # Sum all positives in a batch for normalization and avoid zero
  # num_positives_sum, which would lead to inf loss during training
  num_positives_sum = tf.reduce_sum(labels['mean_num_positives']) + 1.0
  levels = cls_outputs.keys()

  cls_losses = []
  box_losses = []
  box_iou_losses = []
  for level in levels:
    # Onehot encoding for classification labels.
    cls_targets_at_level = tf.one_hot(labels['cls_targets_%d' % level],
                                      params['num_classes'])

    if params['data_format'] == 'channels_first':
      bs, _, width, height, _ = cls_targets_at_level.get_shape().as_list()
      cls_targets_at_level = tf.reshape(cls_targets_at_level,
                                        [bs, -1, width, height])
    else:
      bs, width, height, _, _ = cls_targets_at_level.get_shape().as_list()
      cls_targets_at_level = tf.reshape(cls_targets_at_level,
                                        [bs, width, height, -1])
    box_targets_at_level = labels['box_targets_%d' % level]

    cls_loss = focal_loss(
        cls_outputs[level],
        cls_targets_at_level,
        params['alpha'],
        params['gamma'],
        normalizer=num_positives_sum,
        label_smoothing=params['label_smoothing'])

    if params['data_format'] == 'channels_first':
      cls_loss = tf.reshape(cls_loss,
                            [bs, -1, width, height, params['num_classes']])
    else:
      cls_loss = tf.reshape(cls_loss,
                            [bs, width, height, -1, params['num_classes']])
    cls_loss *= tf.cast(
        tf.expand_dims(tf.not_equal(labels['cls_targets_%d' % level], -2), -1),
        tf.float32)
    cls_losses.append(tf.reduce_sum(cls_loss))

    if params['box_loss_weight']:
      box_losses.append(
          _box_loss(
              box_outputs[level],
              box_targets_at_level,
              num_positives_sum,
              delta=params['delta']))

    if params['iou_loss_type']:
      box_iou_losses.append(
          _box_iou_loss(box_outputs[level], box_targets_at_level,
                        num_positives_sum, params['iou_loss_type']))

  # Sum per level losses to total loss.
  cls_loss = tf.add_n(cls_losses)
  box_loss = tf.add_n(box_losses) if box_losses else 0
  box_iou_loss = tf.add_n(box_iou_losses) if box_iou_losses else 0
  total_loss = (
      cls_loss +
      params['box_loss_weight'] * box_loss +
      params['iou_loss_weight'] * box_iou_loss)

  return total_loss, cls_loss, box_loss, box_iou_loss


def reg_l2_loss(weight_decay, regex=r'.*(kernel|weight):0$'):
  """Return regularization l2 loss loss."""
  var_match = re.compile(regex)
  return weight_decay * tf.add_n([
      tf.nn.l2_loss(v)
      for v in tf.trainable_variables()
      if var_match.match(v.name)
  ])


def _model_fn(features, labels, mode, params, model, variable_filter_fn=None):
  """Model definition entry.

  Args:
    features: the input image tensor with shape [batch_size, height, width, 3].
      The height and width are fixed and equal.
    labels: the input labels in a dictionary. The labels include class targets
      and box targets which are dense label maps. The labels are generated from
      get_input_fn function in data/dataloader.py
    mode: the mode of TPUEstimator including TRAIN, EVAL, and PREDICT.
    params: the dictionary defines hyperparameters of model. The default
      settings are in default_hparams function in this file.
    model: the model outputs class logits and box regression outputs.
    variable_filter_fn: the filter function that takes trainable_variables and
      returns the variable list after applying the filter rule.

  Returns:
    tpu_spec: the TPUEstimatorSpec to run training, evaluation, or prediction.

  Raises:
    RuntimeError: if both ckpt and backbone_ckpt are set.
  """
  utils.image('input_image', features)
  training_hooks = []

  def _model_outputs(inputs):
    # Convert params (dict) to Config for easier access.
    return model(inputs, config=hparams_config.Config(params))

  precision = utils.get_precision(params['strategy'], params['mixed_precision'])
  cls_outputs, box_outputs = utils.build_model_with_precision(
      precision, _model_outputs, features, params['is_training_bn'])

  levels = cls_outputs.keys()
  for level in levels:
    cls_outputs[level] = tf.cast(cls_outputs[level], tf.float32)
    box_outputs[level] = tf.cast(box_outputs[level], tf.float32)

  # First check if it is in PREDICT mode.
  if mode == tf.estimator.ModeKeys.PREDICT:
    predictions = {
        'image': features,
    }
    for level in levels:
      predictions['cls_outputs_%d' % level] = cls_outputs[level]
      predictions['box_outputs_%d' % level] = box_outputs[level]
    return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)

  # Set up training loss and learning rate.
  update_learning_rate_schedule_parameters(params)
  global_step = tf.train.get_or_create_global_step()
  learning_rate = learning_rate_schedule(params, global_step)

  # cls_loss and box_loss are for logging. only total_loss is optimized.
  det_loss, cls_loss, box_loss, box_iou_loss = detection_loss(
      cls_outputs, box_outputs, labels, params)
  reg_l2loss = reg_l2_loss(params['weight_decay'])
  total_loss = det_loss + reg_l2loss

  if mode == tf.estimator.ModeKeys.TRAIN:
    utils.scalar('lrn_rate', learning_rate)
    utils.scalar('trainloss/cls_loss', cls_loss)
    utils.scalar('trainloss/box_loss', box_loss)
    utils.scalar('trainloss/det_loss', det_loss)
    utils.scalar('trainloss/reg_l2_loss', reg_l2loss)
    utils.scalar('trainloss/loss', total_loss)
    if params['iou_loss_type']:
      utils.scalar('trainloss/box_iou_loss', box_iou_loss)

  moving_average_decay = params['moving_average_decay']
  if moving_average_decay:
    ema = tf.train.ExponentialMovingAverage(
        decay=moving_average_decay, num_updates=global_step)
    ema_vars = utils.get_ema_vars()
  if params['strategy'] == 'horovod':
    import horovod.tensorflow as hvd  # pylint: disable=g-import-not-at-top
    learning_rate = learning_rate * hvd.size()
  if mode == tf.estimator.ModeKeys.TRAIN:
    if params['optimizer'].lower() == 'sgd':
      optimizer = tf.train.MomentumOptimizer(
          learning_rate, momentum=params['momentum'])
    elif params['optimizer'].lower() == 'adam':
      optimizer = tf.train.AdamOptimizer(learning_rate)
    else:
      raise ValueError('optimizers should be adam or sgd')

    if params['strategy'] == 'tpu':
      optimizer = tf.tpu.CrossShardOptimizer(optimizer)
    elif params['strategy'] == 'horovod':
      optimizer = hvd.DistributedOptimizer(optimizer)
      training_hooks = [hvd.BroadcastGlobalVariablesHook(0)]

    # Batch norm requires update_ops to be added as a train_op dependency.
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    var_list = tf.trainable_variables()
    if variable_filter_fn:
      var_list = variable_filter_fn(var_list)

    if params.get('clip_gradients_norm', 0) > 0:
      logging.info('clip gradients norm by %f', params['clip_gradients_norm'])
      grads_and_vars = optimizer.compute_gradients(total_loss, var_list)
      with tf.name_scope('clip'):
        grads = [gv[0] for gv in grads_and_vars]
        tvars = [gv[1] for gv in grads_and_vars]
        clipped_grads, gnorm = tf.clip_by_global_norm(
            grads, params['clip_gradients_norm'])
        utils.scalar('gnorm', gnorm)
        grads_and_vars = list(zip(clipped_grads, tvars))

      with tf.control_dependencies(update_ops):
        train_op = optimizer.apply_gradients(grads_and_vars, global_step)
    else:
      with tf.control_dependencies(update_ops):
        train_op = optimizer.minimize(
            total_loss, global_step, var_list=var_list)

    if moving_average_decay:
      with tf.control_dependencies([train_op]):
        train_op = ema.apply(ema_vars)

  else:
    train_op = None

  eval_metrics = None
  if mode == tf.estimator.ModeKeys.EVAL:

    def metric_fn(**kwargs):
      """Returns a dictionary that has the evaluation metrics."""
      if params.get('testdev_dir', None):
        logging.info('Eval testdev_dir %s', params['testdev_dir'])
        eval_metric = coco_metric.EvaluationMetric(
            testdev_dir=params['testdev_dir'])
        coco_metrics = eval_metric.estimator_metric_fn(kwargs['detections_bs'],
                                                       tf.zeros([1]))
      else:
        logging.info('Eval val with groudtruths %s.', params['val_json_file'])
        eval_metric = coco_metric.EvaluationMetric(
            filename=params['val_json_file'])
        coco_metrics = eval_metric.estimator_metric_fn(
            kwargs['detections_bs'], kwargs['groundtruth_data'])

      # Add metrics to output.
      cls_loss = tf.metrics.mean(kwargs['cls_loss_repeat'])
      box_loss = tf.metrics.mean(kwargs['box_loss_repeat'])
      output_metrics = {
          'cls_loss': cls_loss,
          'box_loss': box_loss,
      }
      output_metrics.update(coco_metrics)
      return output_metrics

    cls_loss_repeat = tf.reshape(
        tf.tile(tf.expand_dims(cls_loss, 0), [
            params['batch_size'],
        ]), [params['batch_size'], 1])
    box_loss_repeat = tf.reshape(
        tf.tile(tf.expand_dims(box_loss, 0), [
            params['batch_size'],
        ]), [params['batch_size'], 1])

    params['nms_configs']['max_nms_inputs'] = anchors.MAX_DETECTION_POINTS
    detections_bs = postprocess.generate_detections(params, cls_outputs,
                                                    box_outputs,
                                                    labels['image_scales'],
                                                    labels['source_ids'])

    metric_fn_inputs = {
        'cls_loss_repeat': cls_loss_repeat,
        'box_loss_repeat': box_loss_repeat,
        'source_ids': labels['source_ids'],
        'groundtruth_data': labels['groundtruth_data'],
        'image_scales': labels['image_scales'],
        'detections_bs': detections_bs,
    }
    eval_metrics = (metric_fn, metric_fn_inputs)

  checkpoint = params.get('ckpt') or params.get('backbone_ckpt')

  if checkpoint and mode == tf.estimator.ModeKeys.TRAIN:
    # Initialize the model from an EfficientDet or backbone checkpoint.
    if params.get('ckpt') and params.get('backbone_ckpt'):
      raise RuntimeError(
          '--backbone_ckpt and --checkpoint are mutually exclusive')

    if params.get('backbone_ckpt'):
      var_scope = params['backbone_name'] + '/'
      if params['ckpt_var_scope'] is None:
        # Use backbone name as default checkpoint scope.
        ckpt_scope = params['backbone_name'] + '/'
      else:
        ckpt_scope = params['ckpt_var_scope'] + '/'
    else:
      # Load every var in the given checkpoint
      var_scope = ckpt_scope = '/'

    def scaffold_fn():
      """Loads pretrained model through scaffold function."""
      logging.info('restore variables from %s', checkpoint)

      var_map = utils.get_ckpt_var_map(
          ckpt_path=checkpoint,
          ckpt_scope=ckpt_scope,
          var_scope=var_scope,
          var_exclude_expr=params.get('var_exclude_expr', None))

      tf.train.init_from_checkpoint(checkpoint, var_map)

      return tf.train.Scaffold()
  elif mode == tf.estimator.ModeKeys.EVAL and moving_average_decay:

    def scaffold_fn():
      """Load moving average variables for eval."""
      logging.info('Load EMA vars with ema_decay=%f', moving_average_decay)
      restore_vars_dict = ema.variables_to_restore(ema_vars)
      saver = tf.train.Saver(restore_vars_dict)
      return tf.train.Scaffold(saver=saver)
  else:
    scaffold_fn = None

  if params['strategy'] != 'tpu':
    # Profile every 1K steps.
    profile_hook = tf.train.ProfilerHook(
        save_steps=1000, output_dir=params['model_dir'])
    training_hooks.append(profile_hook)

    # Report memory allocation if OOM
    class OomReportingHook(tf.estimator.SessionRunHook):

      def before_run(self, run_context):
        return tf.estimator.SessionRunArgs(
            fetches=[],
            options=tf.RunOptions(report_tensor_allocations_upon_oom=True))

    training_hooks.append(OomReportingHook())

  return tf.estimator.tpu.TPUEstimatorSpec(
      mode=mode,
      loss=total_loss,
      train_op=train_op,
      eval_metrics=eval_metrics,
      host_call=utils.get_tpu_host_call(global_step, params),
      scaffold_fn=scaffold_fn,
      training_hooks=training_hooks)


def retinanet_model_fn(features, labels, mode, params):
  """RetinaNet model."""
  variable_filter_fn = functools.partial(
      retinanet_arch.remove_variables, resnet_depth=params['resnet_depth'])
  return _model_fn(
      features,
      labels,
      mode,
      params,
      model=retinanet_arch.retinanet,
      variable_filter_fn=variable_filter_fn)


def efficientdet_model_fn(features, labels, mode, params):
  """EfficientDet model."""
  variable_filter_fn = functools.partial(
      efficientdet_arch.freeze_vars, pattern=params['var_freeze_expr'])
  return _model_fn(
      features,
      labels,
      mode,
      params,
      model=efficientdet_arch.efficientdet,
      variable_filter_fn=variable_filter_fn)


def get_model_arch(model_name='efficientdet-d0'):
  """Get model architecture for a given model name."""
  if 'retinanet' in model_name:
    return retinanet_arch.retinanet

  if 'efficientdet' in model_name:
    return efficientdet_arch.efficientdet

  raise ValueError('Invalide model name {}'.format(model_name))


def get_model_fn(model_name='efficientdet-d0'):
  """Get model fn for a given model name."""
  if 'retinanet' in model_name:
    return retinanet_model_fn

  if 'efficientdet' in model_name:
    return efficientdet_model_fn

  raise ValueError('Invalide model name {}'.format(model_name))
