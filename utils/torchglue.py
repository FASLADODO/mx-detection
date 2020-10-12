import mxnet as mx
import mxnet.gluon as gluon


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return gluon.nn.Conv2D(in_channels=in_planes, channels=out_planes, strides=stride,
                     kernel_size=3, padding=1, use_bias=False)


class NoneHybridBlock(mx.gluon.nn.HybridBlock):
    def hybrid_forward(self, F, x, *args, **kwargs):
        raise Exception("unimplemented.")


class nn(object):
    @staticmethod
    def BatchNorm2d(in_planes, momentum):
        return gluon.nn.BatchNorm(in_channels=in_planes, momentum=momentum)

    @staticmethod
    def ReLU(inplace):
        return gluon.nn.Activation(activation="relu")

    @staticmethod
    def Conv2d(in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 bias=True, padding_mode='zeros'):
        assert padding_mode == "zeros"
        return gluon.nn.Conv2D(channels=out_channels, in_channels=in_channels,  kernel_size=kernel_size,
                               strides=stride, padding=padding, dilation=dilation, groups=groups, use_bias=bias)
    @staticmethod
    def Sequential(*args):
        bl = gluon.nn.HybridSequential()
        for a in args:
            bl.add(a)
        return bl

    @staticmethod
    def ModuleList(args):
        bl = gluon.nn.HybridSequential()
        for a in args:
            bl.add(a)
        return bl

    @staticmethod
    def Upsample(scale_factor, mode):
        class _BilinearResize2D(gluon.nn.HybridBlock):
            def hybrid_forward(self, F, x, *args, **kwargs):
                x = F.contrib.BilinearResize2D(x, mode="size",
                                                  scale_height=scale_factor,
                                                  scale_width=scale_factor)
                return x

        return _BilinearResize2D()

    @staticmethod
    def Linear(in_features, out_features, bias=True):
        return gluon.nn.Dense(units=out_features, in_units=in_features, use_bias=bias)

