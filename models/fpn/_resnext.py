# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
# pylint: disable= arguments-differ,missing-docstring
"""ResNext, implemented in Gluon."""
from __future__ import division

__all__ = ['ResNext', 'Block', 'get_resnext',
           'resnext50_32x4d', 'resnext101_32x4d', 'resnext101_64x4d',
           'se_resnext50_32x4d', 'se_resnext101_32x4d', 'se_resnext101_64x4d']

import os
import math
from mxnet import cpu
from mxnet.gluon import nn
from mxnet.gluon.nn import BatchNorm
from mxnet.gluon.block import HybridBlock
import mxnet as mx


class DeformabelConv2D(nn.HybridBlock):
    def __init__(self, num_filter, kernel_size, strides, padding, dilation, use_bias, in_channels, num_groups):
        super(DeformabelConv2D, self).__init__()
        init = mx.init.Zero()
        init.set_verbosity(True)
        with self.name_scope():
            self.conv_offset = nn.Conv2D(channels=72, kernel_size=kernel_size, strides=strides, dilation=dilation,
                                         padding=padding, weight_initializer=init, bias_initializer=init,
                                         prefix="offset_")

            init = mx.init.Normal()
            init.set_verbosity(True)
            self.weight = self.params.get('weight', shape=(num_filter, in_channels//num_groups, kernel_size, kernel_size),
                                          init=init,
                                          allow_deferred_init=True)
        self._num_filter = num_filter
        self._dilation = (dilation, dilation)
        self._padding = (padding, padding)
        self._strides = (strides, strides)
        self._kernel_size = (kernel_size, kernel_size)
        self._use_bias = use_bias
        self._num_groups = num_groups

    def hybrid_forward(self, F, x, weight):
        offset = self.conv_offset(x)
        data = F.contrib.DeformableConvolution(name='fwd',
                                                    data=x,
                                                    offset=offset,
                                                    num_filter=self._num_filter, pad=self._padding,
                                                    kernel=self._kernel_size,
                                                    num_deformable_group=4,
                                                    stride=self._strides, dilate=self._dilation,
                                                    no_bias=not self._use_bias,
                                                    weight=weight,
                                                    num_group=self._num_groups)
        return data

def _conv3x3(in_channels, group_width, kernel_size, strides, padding, groups, use_bias=False):
    return DeformabelConv2D(num_filter=group_width, kernel_size=kernel_size, strides=strides, padding=padding,
                            dilation=1, use_bias=use_bias, in_channels=in_channels, num_groups=groups)


class Block(HybridBlock):
    r"""Bottleneck Block from `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    stride : int
        Stride size.
    downsample : bool, default False
        Whether to downsample the input.
    last_gamma : bool, default False
        Whether to initialize the gamma of the last BatchNorm layer in each bottleneck to zero.
    use_se : bool, default False
        Whether to use Squeeze-and-Excitation module
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    def __init__(self, channels, cardinality, bottleneck_width, stride,
                 downsample=False, last_gamma=False, use_se=False, use_dcn=False,
                 norm_layer=BatchNorm, norm_kwargs=None, **kwargs):
        super(Block, self).__init__(**kwargs)
        D = int(math.floor(channels * (bottleneck_width / 64)))
        group_width = cardinality * D

        self.body = nn.HybridSequential(prefix='')
        self.body.add(nn.Conv2D(group_width, kernel_size=1, use_bias=False))
        self.body.add(norm_layer(**({} if norm_kwargs is None else norm_kwargs)))
        self.body.add(nn.Activation('relu'))
        if use_dcn:
            self.body.add(_conv3x3(group_width, group_width, kernel_size=3, strides=stride, padding=1,
                                   groups=cardinality, use_bias=False))

        else:
            self.body.add(nn.Conv2D(group_width, kernel_size=3, strides=stride, padding=1,
                                    groups=cardinality, use_bias=False))

        self.body.add(norm_layer(**({} if norm_kwargs is None else norm_kwargs)))
        self.body.add(nn.Activation('relu'))
        self.body.add(nn.Conv2D(channels * 4, kernel_size=1, use_bias=False))
        if last_gamma:
            self.body.add(norm_layer(**({} if norm_kwargs is None else norm_kwargs)))
        else:
            self.body.add(norm_layer(gamma_initializer='zeros',
                                     **({} if norm_kwargs is None else norm_kwargs)))

        if use_se:
            self.se = nn.HybridSequential(prefix='')
            self.se.add(nn.Conv2D(channels // 4, kernel_size=1, padding=0))
            self.se.add(nn.Activation('relu'))
            self.se.add(nn.Conv2D(channels * 4, kernel_size=1, padding=0))
            self.se.add(nn.Activation('sigmoid'))
        else:
            self.se = None

        if downsample:
            self.downsample = nn.HybridSequential(prefix='')
            self.downsample.add(nn.Conv2D(channels * 4, kernel_size=1, strides=stride,
                                          use_bias=False))
            self.downsample.add(norm_layer(**({} if norm_kwargs is None else norm_kwargs)))
        else:
            self.downsample = None

    def hybrid_forward(self, F, x):
        residual = x

        x = self.body(x)

        if self.se:
            w = F.contrib.AdaptiveAvgPooling2D(x, output_size=1)
            w = self.se(w)
            x = F.broadcast_mul(x, w)

        if self.downsample:
            residual = self.downsample(residual)

        x = F.Activation(x + residual, act_type='relu')
        return x


# Nets
class ResNext(HybridBlock):
    r"""ResNext model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    layers : list of int
        Numbers of layers in each block
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    classes : int, default 1000
        Number of classification classes.
    last_gamma : bool, default False
        Whether to initialize the gamma of the last BatchNorm layer in each bottleneck to zero.
    use_se : bool, default False
        Whether to use Squeeze-and-Excitation module
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    def __init__(self, layers, cardinality, bottleneck_width,
                 classes=1000, last_gamma=False, use_se=False,
                 norm_layer=BatchNorm, norm_kwargs=None, **kwargs):
        super(ResNext, self).__init__(**kwargs)
        self.cardinality = cardinality
        self.bottleneck_width = bottleneck_width
        channels = 64

        with self.name_scope():
            self.features = nn.HybridSequential(prefix='')
            self.features.add(nn.Conv2D(channels, 7, 2, 3, use_bias=False))

            self.features.add(norm_layer(**({} if norm_kwargs is None else norm_kwargs)))
            self.features.add(nn.Activation('relu'))
            self.features.add(nn.MaxPool2D(3, 2, 1))

            for i, num_layer in enumerate(layers):
                stride = 1 if i == 0 else 2
                self.features.add(self._make_layer(channels, num_layer, stride,
                                                   last_gamma, use_se, i+1,
                                                   norm_layer=norm_layer, norm_kwargs=norm_kwargs,
                                                   use_dcn=i in [1, 2, 3]))
                channels *= 2
            # self.features.add(nn.GlobalAvgPool2D())

            # self.output = nn.Dense(classes)

    def _make_layer(self, channels, num_layers, stride, last_gamma, use_se, stage_index,
                    norm_layer=BatchNorm, norm_kwargs=None, use_dcn=False):
        layer = nn.HybridSequential(prefix='stage%d_'%stage_index)
        with layer.name_scope():
            layer.add(Block(channels, self.cardinality, self.bottleneck_width,
                            stride, True, last_gamma=last_gamma, use_se=use_se, prefix='',
                            norm_layer=norm_layer, norm_kwargs=norm_kwargs, use_dcn=use_dcn))
            for _ in range(num_layers-1):
                layer.add(Block(channels, self.cardinality, self.bottleneck_width,
                                1, False, last_gamma=last_gamma, use_se=use_se, prefix='',
                                norm_layer=norm_layer, norm_kwargs=norm_kwargs, use_dcn=use_dcn))
        return layer

    # pylint: disable=unused-argument
    def hybrid_forward(self, F, x):
        x = self.features(x)
        # x = self.output(x)

        return x


# Specification
resnext_spec = {50: [3, 4, 6, 3],
                101: [3, 4, 23, 3]}


# Constructor
def get_resnext(num_layers, cardinality=32, bottleneck_width=4, use_se=False,
                pretrained=False, ctx=cpu(),
                root=os.path.join('~', '.mxnet', 'models'), **kwargs):
    r"""ResNext model from `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    num_layers : int
        Numbers of layers. Options are 50, 101.
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    assert num_layers in resnext_spec, \
        "Invalid number of layers: %d. Options are %s"%(
            num_layers, str(resnext_spec.keys()))
    layers = resnext_spec[num_layers]
    net = ResNext(layers, cardinality, bottleneck_width, use_se=use_se, **kwargs)
    if pretrained:
        from gluoncv.model_zoo.model_store import get_model_file
        if not use_se:
            net.load_parameters(get_model_file('resnext%d_%dx%dd'%(num_layers, cardinality,
                                                                   bottleneck_width),
                                               tag=pretrained, root=root), ctx=ctx, ignore_extra=True, allow_missing=True)
        else:
            net.load_parameters(get_model_file('se_resnext%d_%dx%dd'%(num_layers, cardinality,
                                                                      bottleneck_width),
                                               tag=pretrained, root=root), ctx=ctx, ignore_extra=True, allow_missing=True)

    return net

def resnext50_32x4d(**kwargs):
    r"""ResNext50 32x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = False
    return get_resnext(50, 32, 4, **kwargs)

def resnext101_32x4d(**kwargs):
    r"""ResNext101 32x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = False
    return get_resnext(101, 32, 4, **kwargs)

def resnext101_64x4d(**kwargs):
    r"""ResNext101 64x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = False
    return get_resnext(101, 64, 4, **kwargs)

def se_resnext50_32x4d(**kwargs):
    r"""SE-ResNext50 32x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = True
    return get_resnext(50, 32, 4, **kwargs)

def se_resnext101_32x4d(**kwargs):
    r"""SE-ResNext101 32x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = True
    return get_resnext(101, 32, 4, **kwargs)

def se_resnext101_64x4d(**kwargs):
    r"""SE-ResNext101 64x4d model from
    `"Aggregated Residual Transformations for Deep Neural Network"
    <http://arxiv.org/abs/1611.05431>`_ paper.

    Parameters
    ----------
    cardinality: int
        Number of groups
    bottleneck_width: int
        Width of bottleneck block
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : Context, default CPU
        The context in which to load the pretrained weights.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    kwargs['use_se'] = True
    return get_resnext(101, 64, 4, **kwargs)
