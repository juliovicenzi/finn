import numpy as np
import warnings
from onnx import TensorProto
from onnx import helper as oh

from finn.custom_op.registry import getCustomOp
from finn.transformation.base import Transformation
from finn.util.fpgadataflow import is_fpgadataflow_node


def _is_fifo_node(node):
    if node.op_type == "StreamingFIFO":
        return True
    else:
        return False


def _suitable_node(node):
    if node is not None:
        if is_fpgadataflow_node(node) is True:
            if _is_fifo_node(node) is False:
                return True
            else:
                return False
        else:
            return False
    else:
        return False


def _suitable_folded_shapes(ishape, oshape):
    matching_stream_width = ishape[-1] == oshape[-1]
    matching_size = np.prod(ishape) == np.prod(oshape)
    return matching_stream_width and matching_size


class InsertFIFO(Transformation):
    """Inserting FIFOs in the beginning and end of the graph as well as
    between fpgadataflow nodes.

    Takes the setting for the depth from the surrounding nodes by extracting
    node attribute 'outFIFODepth' of the previous and node attribute 'inFIFODepth'
    of the subsequent node. max() of these two values sets the FIFO depth.

    Normally, shallow-depth (<=2) FIFOs won't be created since HLS streaming
    interfaces already have a degree of buffering. You can set
    create_shallow_fifos=True to override this default behavior.

    The other node attributes necessary to create a FIFO node are taken from the
    node the FIFO node is inserted after: 'folded_shape' and 'dtype'"""

    def __init__(self, create_shallow_fifos=False):
        super().__init__()
        self.create_shallow_fifos = create_shallow_fifos

    def apply(self, model):
        graph = model.graph
        node_ind = -1
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if _suitable_node(n):
                for n_output in n.output:
                    consumers = model.find_consumers(n_output)
                    if not consumers:
                        continue
                    if len(consumers) > 1:
                        warnings.warn(
                            n.name
                            + ": HLS node with fan-out higher than 1 cannot be stitched"
                        )
                    consumer = consumers[0]
                    if _suitable_node(consumer) is True:
                        n0 = getCustomOp(n)
                        # determine fifo node attributes
                        fld_shape = n0.get_folded_output_shape()
                        dtype = n0.get_output_datatype()

                        # check if folded_shape of output of first node and
                        # input of the second node is equal
                        n1 = getCustomOp(consumer)
                        for idx, inp in enumerate(consumer.input):
                            if inp == n_output:
                                if idx == 0:
                                    fld_shape_2 = n1.get_folded_input_shape()
                                else:
                                    fld_shape_2 = n1.get_folded_input_shape(ind=idx)
                        assert _suitable_folded_shapes(
                            fld_shape, fld_shape_2
                        ), """The
                        folded output shape of the first node is not the same as the
                        folded output shape of the second node. A streaming fifo can't
                        be implemented in between these nodes."""

                        # check if outFIFOdepth attribute of first node
                        # and inFIFOdepth attribute of consumer node is equal
                        n0_depth = n0.get_nodeattr("outFIFODepth")
                        n1_depth = n1.get_nodeattr("inFIFODepth")
                        if n0_depth == n1_depth:
                            fifo_depth = n0_depth
                        elif n0_depth != n1_depth:
                            fifo_depth = max(n0_depth, n1_depth)

                        if fifo_depth > 2 or self.create_shallow_fifos:
                            # assumption: HLS streaming components already have
                            # depth-2 FIFOs on inputs and outputs, so no point
                            # creating additional small FIFOs in between --
                            # we only create the larger FIFOs specified
                            # or unless create_shallow_fifos is specified
                            fifo_output_tensor = oh.make_tensor_value_info(
                                model.make_new_valueinfo_name(),
                                TensorProto.FLOAT,
                                n0.get_normal_output_shape(),
                            )
                            graph.value_info.append(fifo_output_tensor)
                            model.set_tensor_datatype(fifo_output_tensor.name, dtype)

                            fifo_node = oh.make_node(
                                "StreamingFIFO",
                                [n_output],
                                [fifo_output_tensor.name],
                                domain="finn.custom_op.fpgadataflow",
                                backend="fpgadataflow",
                                depth=fifo_depth,
                                folded_shape=fld_shape,
                                dataType=str(dtype.name),
                            )
                            # insert fifo
                            graph.node.insert(node_ind + 1, fifo_node)
                            # set fifo output tensor as new input tensor of second node
                            for idx, inp in enumerate(consumer.input):
                                if inp == n_output:
                                    consumer.input[idx] = fifo_output_tensor.name
                            # ensure created FIFO depth is reflected on both sides
                            n0.set_nodeattr("outFIFODepth", fifo_depth)
                            n1.set_nodeattr("inFIFODepth", fifo_depth)
                            graph_modified = True

        if graph_modified is False:
            # insert FIFO as first node, except when first node is DMA
            if (
                graph.node[0].op_type != "StreamingFIFO"
                and graph.node[0].op_type != "IODMA"
            ):
                n = graph.node[0]
                n_input = n.input[0]
                n0 = getCustomOp(n)
                # determine fifo node attributes
                fld_shape = n0.get_folded_input_shape()
                dtype = n0.get_input_datatype()
                fifo_depth = n0.get_nodeattr("inFIFODepth")

                if fifo_depth <= 2:
                    warnings.warn("Overriding input FIFO depth to 32")
                    fifo_depth = 32

                # create fifo node
                fifo_output_tensor = oh.make_tensor_value_info(
                    model.make_new_valueinfo_name(),
                    TensorProto.FLOAT,
                    n0.get_normal_input_shape(),
                )
                graph.value_info.append(fifo_output_tensor)
                model.set_tensor_datatype(fifo_output_tensor.name, dtype)

                fifo_node = oh.make_node(
                    "StreamingFIFO",
                    [n_input],
                    [fifo_output_tensor.name],
                    domain="finn.custom_op.fpgadataflow",
                    backend="fpgadataflow",
                    depth=fifo_depth,
                    folded_shape=fld_shape,
                    dataType=str(dtype.name),
                )
                # insert fifo
                graph.node.insert(0, fifo_node)

                # set fifo output tensor as new input tensor of second node
                n.input[0] = fifo_output_tensor.name

            # insert FIFO as last node, except when last node is DMA
            graph_out_names = [x.name for x in model.graph.output]
            for graph_out_name in graph_out_names:
                final_node = model.find_producer(graph_out_name)
                if (
                    final_node.op_type != "StreamingFIFO"
                    and final_node.op_type != "IODMA"
                ):
                    assert (
                        final_node.op_type != "TLastMarker"
                    ), """Insert tlast marker should be done
                        after inserting the FIFOs"""
                    n0 = getCustomOp(final_node)
                    # determine fifo node attributes
                    fld_shape = n0.get_folded_output_shape()
                    dtype = n0.get_output_datatype()
                    fifo_depth = n0.get_nodeattr("outFIFODepth")

                    if fifo_depth <= 2:
                        warnings.warn("Overriding output FIFO depth to 32")
                        fifo_depth = 32

                    # create fifo node
                    fifo_input_tensor = oh.make_tensor_value_info(
                        model.make_new_valueinfo_name(),
                        TensorProto.FLOAT,
                        n0.get_normal_output_shape(),
                    )
                    graph.value_info.append(fifo_input_tensor)
                    model.set_tensor_datatype(fifo_input_tensor.name, dtype)

                    fifo_node = oh.make_node(
                        "StreamingFIFO",
                        [fifo_input_tensor.name],
                        [graph_out_name],
                        domain="finn.custom_op.fpgadataflow",
                        backend="fpgadataflow",
                        depth=fifo_depth,
                        folded_shape=fld_shape,
                        dataType=str(dtype.name),
                    )
                    # insert fifo
                    graph.node.append(fifo_node)

                    # set fifo output tensor as new input tensor of second node
                    final_node.output[0] = fifo_input_tensor.name

        return (model, graph_modified)
