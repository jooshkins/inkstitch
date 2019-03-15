from itertools import groupby, chain
import math

import networkx
from shapely import geometry as shgeo

from ..exceptions import InkstitchException
from ..i18n import _
from ..svg import PIXELS_PER_MM
from ..utils.geometry import Point as InkstitchPoint, cut
from .fill import intersect_region_with_grating, stitch_row
from .running_stitch import running_stitch


class InvalidPath(InkstitchException):
    pass


class PathEdge(object):
    OUTLINE_KEYS = ("outline", "extra", "initial")
    SEGMENT_KEY = "segment"

    def __init__(self, nodes, key):
        self.nodes = nodes
        self._sorted_nodes = tuple(sorted(self.nodes))
        self.key = key

    def __getitem__(self, item):
        return self.nodes[item]

    def __hash__(self):
        return hash((self._sorted_nodes, self.key))

    def __eq__(self, other):
        return self._sorted_nodes == other._sorted_nodes and self.key == other.key

    def is_outline(self):
        return self.key in self.OUTLINE_KEYS

    def is_segment(self):
        return self.key == self.SEGMENT_KEY


def auto_fill(shape,
              angle,
              row_spacing,
              end_row_spacing,
              max_stitch_length,
              running_stitch_length,
              staggers,
              skip_last,
              starting_point,
              ending_point=None,
              underpath=True):

    graph = build_graph(shape, angle, row_spacing, end_row_spacing)
    check_graph(graph, shape, max_stitch_length)
    travel_graph = build_travel_graph(graph, shape, angle, underpath)
    path = find_stitch_path(graph, starting_point, ending_point)
    result = path_to_stitches(path, graph, travel_graph, shape, angle, row_spacing, max_stitch_length, running_stitch_length, staggers, skip_last)

    return result


def which_outline(shape, coords):
    """return the index of the outline on which the point resides

    Index 0 is the outer boundary of the fill region.  1+ are the
    outlines of the holes.
    """

    # I'd use an intersection check, but floating point errors make it
    # fail sometimes.

    point = shgeo.Point(*coords)
    outlines = enumerate(list(shape.boundary))
    closest = min(outlines, key=lambda index_outline: index_outline[1].distance(point))

    return closest[0]


def project(shape, coords, outline_index):
    """project the point onto the specified outline

    This returns the distance along the outline at which the point resides.
    """

    outline = list(shape.boundary)[outline_index]
    return outline.project(shgeo.Point(*coords))


def build_graph(shape, angle, row_spacing, end_row_spacing):
    """build a graph representation of the grating segments

    This function builds a specialized graph (as in graph theory) that will
    help us determine a stitching path.  The idea comes from this paper:

    http://www.sciencedirect.com/science/article/pii/S0925772100000158

    The goal is to build a graph that we know must have an Eulerian Path.
    An Eulerian Path is a path from edge to edge in the graph that visits
    every edge exactly once and ends at the node it started at.  Algorithms
    exist to build such a path, and we'll use Hierholzer's algorithm.

    A graph must have an Eulerian Path if every node in the graph has an
    even number of edges touching it.  Our goal here is to build a graph
    that will have this property.

    Based on the paper linked above, we'll build the graph as follows:

        * nodes are the endpoints of the grating segments, where they meet
        with the outer outline of the region the outlines of the interior
        holes in the region.
        * edges are:
        * each section of the outer and inner outlines of the region,
            between nodes
        * double every other edge in the outer and inner hole outlines

    Doubling up on some of the edges seems as if it will just mean we have
    to stitch those spots twice.  This may be true, but it also ensures
    that every node has 4 edges touching it, ensuring that a valid stitch
    path must exist.
    """

    # Convert the shape into a set of parallel line segments.
    rows_of_segments = intersect_region_with_grating(shape, angle, row_spacing, end_row_spacing)
    segments = [segment for row in rows_of_segments for segment in row]

    graph = networkx.MultiGraph()

    # First, add the grating segments as edges.  We'll use the coordinates
    # of the endpoints as nodes, which networkx will add automatically.
    for segment in segments:
        # networkx allows us to label nodes with arbitrary data.  We'll
        # mark this one as a grating segment.
        graph.add_edge(*segment, key="segment")

    for node in graph.nodes():
        outline_index = which_outline(shape, node)
        outline_projection = project(shape, node, outline_index)

        # Tag each node with its index and projection.
        graph.add_node(node, index=outline_index, projection=outline_projection)

    nodes = list(graph.nodes(data=True))  # returns a list of tuples: [(node, {data}), (node, {data}) ...]
    nodes.sort(key=lambda node: (node[1]['index'], node[1]['projection']))

    for outline_index, nodes in groupby(nodes, key=lambda node: node[1]['index']):
        nodes = [node for node, data in nodes]

        # add an edge between each successive node
        for i, (node1, node2) in enumerate(zip(nodes, nodes[1:] + [nodes[0]])):
            graph.add_edge(node1, node2, key="outline")

            # duplicate every other edge
            if i % 2 == 0:
                graph.add_edge(node1, node2, key="extra")

    return graph


def build_travel_graph(top_stitch_graph, shape, top_stitch_angle, underpath):
    graph = networkx.Graph()
    graph.add_nodes_from(top_stitch_graph.nodes(data=True))

    if underpath:
        # need to concatenate all the rows
        grating1 = shgeo.MultiLineString(list(chain(*intersect_region_with_grating(shape, top_stitch_angle + math.pi / 4, 2 * PIXELS_PER_MM))))
        grating2 = shgeo.MultiLineString(list(chain(*intersect_region_with_grating(shape, top_stitch_angle - math.pi / 4, 2 * PIXELS_PER_MM))))

        endpoints = [coord for mls in (grating1, grating2)
                     for ls in mls
                     for coord in ls.coords]

        for node in endpoints:
            outline_index = which_outline(shape, node)
            outline_projection = project(shape, node, outline_index)

            # Tag each node with its index and projection.
            graph.add_node(node, index=outline_index, projection=outline_projection)

    nodes = list(graph.nodes(data=True))  # returns a list of tuples: [(node, {data}), (node, {data}) ...]
    nodes.sort(key=lambda node: (node[1]['index'], node[1]['projection']))

    for outline_index, nodes in groupby(nodes, key=lambda node: node[1]['index']):
        nodes = [node for node, data in nodes]

        # add an edge between each successive node
        for node1, node2 in zip(nodes, nodes[1:] + [nodes[0]]):
            p1 = InkstitchPoint(*node1)
            p2 = InkstitchPoint(*node2)
            graph.add_edge(node1, node2, weight=3 * p1.distance(p2))

    if underpath:
        interior_edges = grating1.symmetric_difference(grating2)
        for ls in interior_edges.geoms:
            p1, p2 = [InkstitchPoint(*coord) for coord in ls.coords]

            graph.add_edge(p1.as_tuple(), p2.as_tuple(), weight=p1.distance(p2))

    return graph


def check_graph(graph, shape, max_stitch_length):
    if networkx.is_empty(graph) or not networkx.is_eulerian(graph):
        if shape.area < max_stitch_length ** 2:
            raise InvalidPath(_("This shape is so small that it cannot be filled with rows of stitches.  "
                                "It would probably look best as a satin column or running stitch."))
        else:
            raise InvalidPath(_("Cannot parse shape.  "
                                "This most often happens because your shape is made up of multiple sections that aren't connected."))


def nearest_node_on_outline(graph, point, outline_index=0):
    point = shgeo.Point(*point)
    outline_nodes = [node for node, data in graph.nodes(data=True) if data['index'] == outline_index]
    nearest = min(outline_nodes, key=lambda node: shgeo.Point(*node).distance(point))

    return nearest


def find_stitch_path(graph, starting_point=None, ending_point=None):
    """find a path that visits every grating segment exactly once

    Theoretically, we just need to find an Eulerian Path in the graph.
    However, we don't actually care whether every single edge is visited.
    The edges on the outline of the region are only there to help us get
    from one grating segment to the next.

    We'll build a Eulerian Path using Hierholzer's algorithm.  A true
    Eulerian Path would visit every single edge (including all the extras
    we inserted in build_graph()),but we'll stop short once we've visited
    every grating segment since that's all we really care about.

    Hierholzer's algorithm says to select an arbitrary starting node at
    each step.  In order to produce a reasonable stitch path, we'll select
    the starting node carefully such that we get back-and-forth traversal like
    mowing a lawn.

    To do this, we'll use a simple heuristic: try to start from nodes in
    the order of most-recently-visited first.
    """

    graph = graph.copy()

    if starting_point is None:
        starting_point = graph.nodes.keys()[0]

    starting_node = nearest_node_on_outline(graph, starting_point)

    if ending_point is None:
        ending_node = starting_node
    else:
        ending_node = nearest_node_on_outline(graph, ending_point)

    # The algorithm below is adapted from networkx.eulerian_circuit().
    path = []
    vertex_stack = [(ending_node, None)]
    last_vertex = None
    last_key = None

    while vertex_stack:
        current_vertex, current_key = vertex_stack[-1]
        if graph.degree(current_vertex) == 0:
            if last_vertex is not None:
                path.append(PathEdge((last_vertex, current_vertex), last_key))
            last_vertex, last_key = current_vertex, current_key
            vertex_stack.pop()
        else:
            ignore, next_vertex, next_key = pick_edge(graph.edges(current_vertex, keys=True))
            vertex_stack.append((next_vertex, next_key))
            graph.remove_edge(current_vertex, next_vertex, next_key)

    # The above has the excellent property that it tends to do travel stitches
    # before the rows in that area, so we can hide the travel stitches under
    # the rows.
    #
    # The only downside is that the path is a loop starting and ending at the
    # ending node.  We need to start at the starting node, so we'll just
    # start off by traveling to the ending node.
    #
    # Note, it's quite possible that part of this PathEdge will be eliminated by
    # collapse_sequential_outline_edges().

    if starting_node is not ending_node:
        path.insert(0, PathEdge((starting_node, ending_node), key="initial"))

    return path


def pick_edge(edges):
    """Pick the next edge to traverse in the pathfinding algorithm"""

    # Prefer a segment if one is available.  This has the effect of
    # creating long sections of back-and-forth row traversal.
    for source, node, key in edges:
        if key == 'segment':
            return source, node, key

    return list(edges)[0]


def collapse_sequential_outline_edges(path):
    """collapse sequential edges that fall on the same outline

    When the path follows multiple edges along the outline of the region,
    replace those edges with the starting and ending points.  We'll use
    these to stitch along the outline later on.
    """

    start_of_run = None
    new_path = []

    for edge in path:
        if edge.is_segment():
            if start_of_run:
                # close off the last run
                new_path.append(PathEdge((start_of_run, edge[0]), "collapsed"))
                start_of_run = None

            new_path.append(edge)
        else:
            if not start_of_run:
                start_of_run = edge[0]

    if start_of_run:
        # if we were still in a run, close it off
        new_path.append(PathEdge((start_of_run, edge[1]), "collapsed"))

    return new_path


def travel(graph, travel_graph, shape, start, end, running_stitch_length, row_spacing):
    """Create stitches to get from one point on an outline of the shape to another."""

    path = networkx.shortest_path(travel_graph, start, end, weight='weight')
    path = [InkstitchPoint(*p) for p in path]
    stitches = running_stitch(path, running_stitch_length)

    # The row of stitches already stitched the first point, so skip it.
    return stitches[1:]


def path_to_stitches(path, graph, travel_graph, shape, angle, row_spacing, max_stitch_length, running_stitch_length, staggers, skip_last):
    path = collapse_sequential_outline_edges(path)

    stitches = []

    for edge in path:
        if edge.is_segment():
            stitch_row(stitches, edge[0], edge[1], angle, row_spacing, max_stitch_length, staggers, skip_last)
        else:
            stitches.extend(travel(graph, travel_graph, shape, edge[0], edge[1], running_stitch_length, row_spacing))

    return stitches
