// SPDX-License-Identifier: GPL-3.0-or-later
// Device-resident CUDA implementation of Haori's square-lattice cloth solver.

#include "solver.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <numeric>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace hsc {
namespace {

constexpr float kEpsilon = 1.0e-8F;
constexpr float kCollisionSearch = 0.040001F;
constexpr int32_t kThreads = 256;
constexpr int32_t kBvhStack = 64;

void check_cuda(cudaError_t status, const char* operation) {
    if (status != cudaSuccess) {
        throw std::runtime_error(
            std::string(operation) + ": " + cudaGetErrorString(status));
    }
}

void validate_index(int32_t index, int32_t size, const char* label) {
    if (index < 0 || index >= size) {
        throw std::out_of_range(std::string(label) + " index is out of range");
    }
}

bool finite3(const float* values) {
    return std::isfinite(values[0]) && std::isfinite(values[1]) && std::isfinite(values[2]);
}

struct Seam {
    int32_t a;
    int32_t b;
    float target_length;
    int32_t captured;
};

struct Edge {
    int32_t a;
    int32_t b;
    float rest_length;
};

struct Quad {
    int4 vertices;
    float rest_u_squared;
    float rest_v_squared;
    float rest_shear;
};

struct Bend {
    int3 vertices;
    float previous_rest_length;
    float next_rest_length;
};

struct Face {
    int3 vertices;
};

struct BvhNode {
    float3 minimum;
    float3 maximum;
    int32_t left;
    int32_t right;
    int32_t parent;
    int32_t face;
};

struct DeviceStats {
    int32_t substeps;
    int32_t iterations;
    int32_t seam_count;
    int32_t captured_seam_count;
    int32_t edge_count;
    int32_t quad_count;
    int32_t bend_count;
    int32_t body_candidate_count;
    uint32_t maximum_displacement_bits;
    int32_t nonfinite;
};

struct DeviceData {
    hsc_config config;
    float3 gravity;
    int32_t vertex_count;
    int32_t seam_count;
    int32_t edge_count;
    int32_t quad_count;
    int32_t bend_count;
    int32_t body_vertex_count;
    int32_t body_face_count;
    int32_t bvh_node_count;
    int32_t seam_color_count;
    int32_t edge_color_count;
    int32_t quad_color_count;
    int32_t bend_color_count;

    float3* positions;
    float3* previous;
    float3* velocities;
    float* inverse_masses;
    int32_t* locked;
    int32_t* seam_driven;
    int32_t* contact_flags;
    int32_t* candidate_faces;
    float3* click_start;

    Seam* seams;
    Edge* edges;
    Quad* quads;
    Bend* bends;
    int32_t* seam_color_offsets;
    int32_t* edge_color_offsets;
    int32_t* quad_color_offsets;
    int32_t* bend_color_offsets;

    float3* body_positions;
    Face* body_faces;
    BvhNode* bvh_nodes;
    int32_t* bvh_leaf_nodes;
    int32_t* bvh_ready;
    DeviceStats* stats;
};

template <typename T>
struct DeviceBuffer {
    T* pointer = nullptr;
    size_t count = 0;

    DeviceBuffer() = default;
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;

    ~DeviceBuffer() {
        release();
    }

    void release() noexcept {
        if (pointer != nullptr) {
            (void)cudaFree(pointer);
            pointer = nullptr;
            count = 0;
        }
    }

    void allocate(size_t requested) {
        if (requested == 0) {
            return;
        }
        check_cuda(cudaMalloc(reinterpret_cast<void**>(&pointer), requested * sizeof(T)), "cudaMalloc");
        count = requested;
    }

    void upload(const std::vector<T>& values) {
        allocate(values.size());
        if (!values.empty()) {
            check_cuda(
                cudaMemcpy(pointer, values.data(), values.size() * sizeof(T), cudaMemcpyHostToDevice),
                "cudaMemcpy host to device");
        }
    }
};

template <typename T, typename Vertices>
std::pair<std::vector<T>, std::vector<int32_t>> color_constraints(
    const std::vector<T>& constraints,
    int32_t vertex_count,
    Vertices&& vertices_for) {
    if (constraints.empty()) {
        return {{}, {0}};
    }

    std::vector<std::vector<int32_t>> colors_by_vertex(static_cast<size_t>(vertex_count));
    std::vector<int32_t> assigned(constraints.size(), 0);
    int32_t color_count = 0;
    for (size_t index = 0; index < constraints.size(); ++index) {
        std::vector<uint8_t> forbidden(static_cast<size_t>(color_count), 0);
        for (const int32_t vertex : vertices_for(constraints[index])) {
            for (const int32_t color : colors_by_vertex[static_cast<size_t>(vertex)]) {
                forbidden[static_cast<size_t>(color)] = 1;
            }
        }
        int32_t color = 0;
        while (color < color_count && forbidden[static_cast<size_t>(color)] != 0) {
            ++color;
        }
        if (color == color_count) {
            ++color_count;
        }
        assigned[index] = color;
        for (const int32_t vertex : vertices_for(constraints[index])) {
            colors_by_vertex[static_cast<size_t>(vertex)].push_back(color);
        }
    }

    std::vector<int32_t> offsets(static_cast<size_t>(color_count + 1), 0);
    for (const int32_t color : assigned) {
        ++offsets[static_cast<size_t>(color + 1)];
    }
    std::partial_sum(offsets.begin(), offsets.end(), offsets.begin());
    std::vector<int32_t> cursor = offsets;
    std::vector<T> reordered(constraints.size());
    for (size_t index = 0; index < constraints.size(); ++index) {
        const int32_t color = assigned[index];
        reordered[static_cast<size_t>(cursor[static_cast<size_t>(color)]++)] = constraints[index];
    }
    return {std::move(reordered), std::move(offsets)};
}

float3 host_point(const float* positions, int32_t index) {
    return make_float3(
        positions[index * 3],
        positions[index * 3 + 1],
        positions[index * 3 + 2]);
}

float host_axis(const float3& value, int32_t axis) {
    return axis == 0 ? value.x : (axis == 1 ? value.y : value.z);
}

struct HostBvhBuilder {
    const std::vector<float3>& centroids;
    std::vector<BvhNode> nodes;
    std::vector<int32_t> leaf_nodes;

    int32_t build(std::vector<int32_t>& faces, size_t begin, size_t end, int32_t parent) {
        const int32_t node_index = static_cast<int32_t>(nodes.size());
        nodes.push_back({});
        nodes[static_cast<size_t>(node_index)].parent = parent;
        nodes[static_cast<size_t>(node_index)].left = -1;
        nodes[static_cast<size_t>(node_index)].right = -1;
        nodes[static_cast<size_t>(node_index)].face = -1;
        if (end - begin == 1) {
            const int32_t face = faces[begin];
            nodes[static_cast<size_t>(node_index)].face = face;
            leaf_nodes[static_cast<size_t>(face)] = node_index;
            return node_index;
        }

        float3 minimum = make_float3(FLT_MAX, FLT_MAX, FLT_MAX);
        float3 maximum = make_float3(-FLT_MAX, -FLT_MAX, -FLT_MAX);
        for (size_t index = begin; index < end; ++index) {
            const float3 value = centroids[static_cast<size_t>(faces[index])];
            minimum.x = std::min(minimum.x, value.x);
            minimum.y = std::min(minimum.y, value.y);
            minimum.z = std::min(minimum.z, value.z);
            maximum.x = std::max(maximum.x, value.x);
            maximum.y = std::max(maximum.y, value.y);
            maximum.z = std::max(maximum.z, value.z);
        }
        const float3 extent = make_float3(
            maximum.x - minimum.x,
            maximum.y - minimum.y,
            maximum.z - minimum.z);
        const int32_t axis = extent.y > extent.x
            ? (extent.z > extent.y ? 2 : 1)
            : (extent.z > extent.x ? 2 : 0);
        const size_t middle = begin + (end - begin) / 2;
        std::nth_element(
            faces.begin() + static_cast<std::ptrdiff_t>(begin),
            faces.begin() + static_cast<std::ptrdiff_t>(middle),
            faces.begin() + static_cast<std::ptrdiff_t>(end),
            [&](int32_t left, int32_t right) {
                return host_axis(centroids[static_cast<size_t>(left)], axis) <
                    host_axis(centroids[static_cast<size_t>(right)], axis);
            });
        const int32_t left = build(faces, begin, middle, node_index);
        const int32_t right = build(faces, middle, end, node_index);
        nodes[static_cast<size_t>(node_index)].left = left;
        nodes[static_cast<size_t>(node_index)].right = right;
        return node_index;
    }
};

std::pair<std::vector<BvhNode>, std::vector<int32_t>> build_body_bvh(
    const std::vector<float3>& positions,
    const std::vector<Face>& body_faces) {
    if (body_faces.empty()) {
        return {{}, {}};
    }
    std::vector<float3> centroids(body_faces.size());
    for (size_t index = 0; index < body_faces.size(); ++index) {
        const int3 face = body_faces[index].vertices;
        const float3 a = positions[static_cast<size_t>(face.x)];
        const float3 b = positions[static_cast<size_t>(face.y)];
        const float3 c = positions[static_cast<size_t>(face.z)];
        centroids[index] = make_float3(
            (a.x + b.x + c.x) / 3.0F,
            (a.y + b.y + c.y) / 3.0F,
            (a.z + b.z + c.z) / 3.0F);
    }
    std::vector<int32_t> faces(body_faces.size());
    std::iota(faces.begin(), faces.end(), 0);
    HostBvhBuilder builder{centroids, {}, std::vector<int32_t>(body_faces.size(), -1)};
    builder.nodes.reserve(body_faces.size() * 2 - 1);
    builder.build(faces, 0, faces.size(), -1);
    return {std::move(builder.nodes), std::move(builder.leaf_nodes)};
}

__host__ __device__ float3 add3(const float3& a, const float3& b) {
    return make_float3(a.x + b.x, a.y + b.y, a.z + b.z);
}

__host__ __device__ float3 sub3(const float3& a, const float3& b) {
    return make_float3(a.x - b.x, a.y - b.y, a.z - b.z);
}

__host__ __device__ float3 mul3(const float3& value, float scalar) {
    return make_float3(value.x * scalar, value.y * scalar, value.z * scalar);
}

__host__ __device__ float dot3(const float3& a, const float3& b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}

__host__ __device__ float3 cross3(const float3& a, const float3& b) {
    return make_float3(
        a.y * b.z - a.z * b.y,
        a.z * b.x - a.x * b.z,
        a.x * b.y - a.y * b.x);
}

__host__ __device__ float length_squared3(const float3& value) {
    return dot3(value, value);
}

__host__ __device__ float length3(const float3& value) {
    return sqrtf(length_squared3(value));
}

__device__ float3 normalized3(const float3& value) {
    const float magnitude = length3(value);
    if (!(magnitude > kEpsilon) || !isfinite(magnitude)) {
        return make_float3(0.0F, 0.0F, 1.0F);
    }
    return mul3(value, 1.0F / magnitude);
}

__device__ float3 clamp_length3(const float3& value, float maximum) {
    const float squared = length_squared3(value);
    if (maximum > 0.0F && squared > maximum * maximum) {
        return mul3(value, maximum * rsqrtf(squared));
    }
    return value;
}

__device__ float inverse_mass(const DeviceData& data, int32_t vertex) {
    return data.locked[vertex] == 0 ? data.inverse_masses[vertex] : 0.0F;
}

__device__ float3 closest_triangle_point(
    const float3& point,
    const float3& a,
    const float3& b,
    const float3& c) {
    const float3 ab = sub3(b, a);
    const float3 ac = sub3(c, a);
    const float3 ap = sub3(point, a);
    const float d1 = dot3(ab, ap);
    const float d2 = dot3(ac, ap);
    if (d1 <= 0.0F && d2 <= 0.0F) {
        return a;
    }
    const float3 bp = sub3(point, b);
    const float d3 = dot3(ab, bp);
    const float d4 = dot3(ac, bp);
    if (d3 >= 0.0F && d4 <= d3) {
        return b;
    }
    const float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0F && d1 >= 0.0F && d3 <= 0.0F) {
        return add3(a, mul3(ab, d1 / (d1 - d3)));
    }
    const float3 cp = sub3(point, c);
    const float d5 = dot3(ab, cp);
    const float d6 = dot3(ac, cp);
    if (d6 >= 0.0F && d5 <= d6) {
        return c;
    }
    const float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0F && d2 >= 0.0F && d6 <= 0.0F) {
        return add3(a, mul3(ac, d2 / (d2 - d6)));
    }
    const float va = d3 * d6 - d5 * d4;
    if (va <= 0.0F && (d4 - d3) >= 0.0F && (d5 - d6) >= 0.0F) {
        return add3(b, mul3(sub3(c, b), (d4 - d3) / ((d4 - d3) + (d5 - d6))));
    }
    const float inverse = 1.0F / (va + vb + vc);
    return add3(a, add3(mul3(ab, vb * inverse), mul3(ac, vc * inverse)));
}

__device__ float aabb_distance_squared(const float3& point, const BvhNode& node) {
    float result = 0.0F;
    const float values[3]{point.x, point.y, point.z};
    const float minimum[3]{node.minimum.x, node.minimum.y, node.minimum.z};
    const float maximum[3]{node.maximum.x, node.maximum.y, node.maximum.z};
    for (int axis = 0; axis < 3; ++axis) {
        const float delta = values[axis] < minimum[axis]
            ? minimum[axis] - values[axis]
            : (values[axis] > maximum[axis] ? values[axis] - maximum[axis] : 0.0F);
        result += delta * delta;
    }
    return result;
}

__device__ bool ray_aabb(
    const float3& origin,
    const float3& direction,
    float maximum_distance,
    const BvhNode& node) {
    float near_value = 0.0F;
    float far_value = maximum_distance;
    const float origins[3]{origin.x, origin.y, origin.z};
    const float directions[3]{direction.x, direction.y, direction.z};
    const float minimum[3]{node.minimum.x, node.minimum.y, node.minimum.z};
    const float maximum[3]{node.maximum.x, node.maximum.y, node.maximum.z};
    for (int axis = 0; axis < 3; ++axis) {
        if (fabsf(directions[axis]) < 1.0e-12F) {
            if (origins[axis] < minimum[axis] || origins[axis] > maximum[axis]) {
                return false;
            }
            continue;
        }
        const float inverse = 1.0F / directions[axis];
        float first = (minimum[axis] - origins[axis]) * inverse;
        float second = (maximum[axis] - origins[axis]) * inverse;
        if (first > second) {
            const float temporary = first;
            first = second;
            second = temporary;
        }
        near_value = fmaxf(near_value, first);
        far_value = fminf(far_value, second);
        if (near_value > far_value) {
            return false;
        }
    }
    return far_value > 1.0e-7F;
}

__device__ bool ray_triangle(
    const float3& origin,
    const float3& direction,
    float maximum_distance,
    const float3& a,
    const float3& b,
    const float3& c) {
    const float3 edge1 = sub3(b, a);
    const float3 edge2 = sub3(c, a);
    const float3 p = cross3(direction, edge2);
    const float determinant = dot3(edge1, p);
    if (fabsf(determinant) < 1.0e-9F) {
        return false;
    }
    const float inverse = 1.0F / determinant;
    const float3 t = sub3(origin, a);
    const float u = dot3(t, p) * inverse;
    if (u < 0.0F || u > 1.0F) {
        return false;
    }
    const float3 q = cross3(t, edge1);
    const float v = dot3(direction, q) * inverse;
    if (v < 0.0F || u + v > 1.0F) {
        return false;
    }
    const float distance = dot3(edge2, q) * inverse;
    return distance > 1.0e-7F && distance <= maximum_distance;
}

__device__ int32_t nearest_body_face(const DeviceData& data, const float3& point, float& best_squared) {
    if (data.bvh_node_count == 0) {
        return -1;
    }
    int32_t stack[kBvhStack];
    int32_t size = 0;
    stack[size++] = 0;
    int32_t best_face = -1;
    best_squared = FLT_MAX;
    while (size > 0) {
        const int32_t node_index = stack[--size];
        const BvhNode node = data.bvh_nodes[node_index];
        if (aabb_distance_squared(point, node) > best_squared) {
            continue;
        }
        if (node.face >= 0) {
            const int3 indices = data.body_faces[node.face].vertices;
            const float3 closest = closest_triangle_point(
                point,
                data.body_positions[indices.x],
                data.body_positions[indices.y],
                data.body_positions[indices.z]);
            const float squared = length_squared3(sub3(point, closest));
            if (squared < best_squared) {
                best_squared = squared;
                best_face = node.face;
            }
            continue;
        }
        const float left_distance = aabb_distance_squared(point, data.bvh_nodes[node.left]);
        const float right_distance = aabb_distance_squared(point, data.bvh_nodes[node.right]);
        const int32_t near_child = left_distance <= right_distance ? node.left : node.right;
        const int32_t far_child = left_distance <= right_distance ? node.right : node.left;
        if (size + 2 <= kBvhStack) {
            stack[size++] = far_child;
            stack[size++] = near_child;
        }
    }
    return best_face;
}

__device__ int32_t ray_intersection_count(
    const DeviceData& data,
    const float3& point,
    const float3& direction,
    float maximum_distance) {
    int32_t stack[kBvhStack];
    int32_t size = 0;
    int32_t count = 0;
    stack[size++] = 0;
    while (size > 0) {
        const BvhNode node = data.bvh_nodes[stack[--size]];
        if (!ray_aabb(point, direction, maximum_distance, node)) {
            continue;
        }
        if (node.face >= 0) {
            const int3 indices = data.body_faces[node.face].vertices;
            if (ray_triangle(
                    point,
                    direction,
                    maximum_distance,
                    data.body_positions[indices.x],
                    data.body_positions[indices.y],
                    data.body_positions[indices.z])) {
                ++count;
            }
            continue;
        }
        if (size + 2 <= kBvhStack) {
            stack[size++] = node.left;
            stack[size++] = node.right;
        }
    }
    return count;
}

__global__ void refit_bvh_kernel(DeviceData* data) {
    const int32_t face_index = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (face_index >= data->body_face_count) {
        return;
    }
    const int32_t node_index = data->bvh_leaf_nodes[face_index];
    const int3 face = data->body_faces[face_index].vertices;
    const float3 a = data->body_positions[face.x];
    const float3 b = data->body_positions[face.y];
    const float3 c = data->body_positions[face.z];
    BvhNode& leaf = data->bvh_nodes[node_index];
    leaf.minimum = make_float3(
        fminf(a.x, fminf(b.x, c.x)),
        fminf(a.y, fminf(b.y, c.y)),
        fminf(a.z, fminf(b.z, c.z)));
    leaf.maximum = make_float3(
        fmaxf(a.x, fmaxf(b.x, c.x)),
        fmaxf(a.y, fmaxf(b.y, c.y)),
        fmaxf(a.z, fmaxf(b.z, c.z)));

    int32_t current = node_index;
    while (data->bvh_nodes[current].parent >= 0) {
        __threadfence();
        const int32_t parent_index = data->bvh_nodes[current].parent;
        const int32_t arrival = atomicAdd(&data->bvh_ready[parent_index], 1);
        if (arrival == 0) {
            break;
        }
        const BvhNode parent = data->bvh_nodes[parent_index];
        const BvhNode left = data->bvh_nodes[parent.left];
        const BvhNode right = data->bvh_nodes[parent.right];
        data->bvh_nodes[parent_index].minimum = make_float3(
            fminf(left.minimum.x, right.minimum.x),
            fminf(left.minimum.y, right.minimum.y),
            fminf(left.minimum.z, right.minimum.z));
        data->bvh_nodes[parent_index].maximum = make_float3(
            fmaxf(left.maximum.x, right.maximum.x),
            fmaxf(left.maximum.y, right.maximum.y),
            fmaxf(left.maximum.z, right.maximum.z));
        current = parent_index;
    }
}

__global__ void initialize_stats_kernel(DeviceData* data, int32_t iterations, int32_t candidates) {
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        DeviceStats& stats = *data->stats;
        stats.substeps = data->config.substeps;
        stats.iterations = iterations;
        stats.seam_count = data->seam_count;
        stats.captured_seam_count = 0;
        stats.edge_count = data->edge_count;
        stats.quad_count = data->quad_count;
        stats.bend_count = data->bend_count;
        stats.body_candidate_count = candidates;
        stats.maximum_displacement_bits = 0;
        stats.nonfinite = 0;
    }
}

__global__ void find_body_candidates_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex >= data->vertex_count) {
        return;
    }
    data->candidate_faces[vertex] = -1;
    if (data->locked[vertex] != 0 || data->bvh_node_count == 0) {
        return;
    }
    const BvhNode root = data->bvh_nodes[0];
    const float3 point = data->positions[vertex];
    if (
        point.x < root.minimum.x - kCollisionSearch || point.x > root.maximum.x + kCollisionSearch ||
        point.y < root.minimum.y - kCollisionSearch || point.y > root.maximum.y + kCollisionSearch ||
        point.z < root.minimum.z - kCollisionSearch || point.z > root.maximum.z + kCollisionSearch) {
        return;
    }
    float nearest_squared = FLT_MAX;
    const int32_t nearest = nearest_body_face(*data, point, nearest_squared);
    if (nearest < 0) {
        return;
    }
    bool candidate = nearest_squared <= kCollisionSearch * kCollisionSearch;
    if (!candidate) {
        const float3 extent = sub3(root.maximum, root.minimum);
        const float ray_distance = fmaxf(length3(extent) * 2.0F, 1.0F);
        const float3 directions[3]{
            normalized3(make_float3(1.0F, 0.371F, 0.529F)),
            normalized3(make_float3(-0.417F, 1.0F, 0.263F)),
            normalized3(make_float3(0.193F, -0.487F, 1.0F)),
        };
        int32_t odd_votes = 0;
        for (const float3 direction : directions) {
            odd_votes += ray_intersection_count(*data, point, direction, ray_distance) & 1;
        }
        candidate = odd_votes >= 2;
    }
    if (candidate) {
        data->candidate_faces[vertex] = nearest;
        atomicAdd(&data->stats->body_candidate_count, 1);
    }
}

__device__ void project_distance(
    DeviceData& data,
    int32_t a_index,
    int32_t b_index,
    float target_length,
    float relaxation) {
    if (!(relaxation > 0.0F)) {
        return;
    }
    const float a_weight = inverse_mass(data, a_index);
    const float b_weight = inverse_mass(data, b_index);
    const float weight_sum = a_weight + b_weight;
    if (!(weight_sum > 0.0F)) {
        return;
    }
    const float3 difference = sub3(data.positions[b_index], data.positions[a_index]);
    const float current_length = length3(difference);
    if (!(current_length > kEpsilon)) {
        return;
    }
    const float3 direction = mul3(difference, 1.0F / current_length);
    const float scaled_error = relaxation * (current_length - target_length) / weight_sum;
    if (a_weight > 0.0F) {
        data.positions[a_index] = add3(
            data.positions[a_index],
            clamp_length3(
                mul3(direction, a_weight * scaled_error),
                data.config.maximum_position_correction));
    }
    if (b_weight > 0.0F) {
        data.positions[b_index] = sub3(
            data.positions[b_index],
            clamp_length3(
                mul3(direction, b_weight * scaled_error),
                data.config.maximum_position_correction));
    }
}

__device__ void project_edge(DeviceData& data, const Edge& edge) {
    const float3 difference = sub3(data.positions[edge.b], data.positions[edge.a]);
    const float current_length = length3(difference);
    if (!(current_length > kEpsilon)) {
        return;
    }
    const float slack = edge.rest_length * data.config.stretch_limit;
    const bool beyond_reserve =
        current_length > edge.rest_length + slack || current_length < edge.rest_length - slack;
    const float relaxation = beyond_reserve ? 1.0F : data.config.stretch_relaxation;
    project_distance(data, edge.a, edge.b, edge.rest_length, relaxation);
}

__device__ void project_quad(DeviceData& data, const Quad& quad) {
    const int32_t indices[4]{
        quad.vertices.x, quad.vertices.y, quad.vertices.z, quad.vertices.w};
    float weights[4]{};
    for (int corner = 0; corner < 4; ++corner) {
        weights[corner] = inverse_mass(data, indices[corner]);
    }
    const float3 x0 = data.positions[indices[0]];
    const float3 x1 = data.positions[indices[1]];
    const float3 x2 = data.positions[indices[2]];
    const float3 x3 = data.positions[indices[3]];
    const float3 u = mul3(add3(sub3(x1, x0), sub3(x2, x3)), 0.5F);
    const float3 v = mul3(add3(sub3(x3, x0), sub3(x2, x1)), 0.5F);
    const float value = dot3(u, v) - quad.rest_shear;
    const float3 gradients[4]{
        mul3(add3(u, v), -0.5F),
        mul3(sub3(v, u), 0.5F),
        mul3(add3(u, v), 0.5F),
        mul3(sub3(u, v), 0.5F),
    };
    float denominator = 0.0F;
    for (int corner = 0; corner < 4; ++corner) {
        denominator += weights[corner] * length_squared3(gradients[corner]);
    }
    if (!(denominator > kEpsilon * kEpsilon)) {
        return;
    }
    const float multiplier = -data.config.shear_relaxation * value / denominator;
    for (int corner = 0; corner < 4; ++corner) {
        if (weights[corner] > 0.0F) {
            data.positions[indices[corner]] = add3(
                data.positions[indices[corner]],
                clamp_length3(
                    mul3(gradients[corner], weights[corner] * multiplier),
                    data.config.maximum_position_correction));
        }
    }
}

__device__ void project_bend(DeviceData& data, const Bend& bend) {
    const int32_t indices[3]{bend.vertices.x, bend.vertices.y, bend.vertices.z};
    const float previous_coefficient = 1.0F / bend.previous_rest_length;
    const float next_coefficient = 1.0F / bend.next_rest_length;
    const float coefficients[3]{
        previous_coefficient,
        -(previous_coefficient + next_coefficient),
        next_coefficient,
    };
    float weights[3]{};
    float denominator = 0.0F;
    float3 curvature = make_float3(0.0F, 0.0F, 0.0F);
    for (int point = 0; point < 3; ++point) {
        weights[point] = inverse_mass(data, indices[point]);
        denominator += weights[point] * coefficients[point] * coefficients[point];
        curvature = add3(curvature, mul3(data.positions[indices[point]], coefficients[point]));
    }
    if (!(denominator > kEpsilon)) {
        return;
    }
    for (int point = 0; point < 3; ++point) {
        if (weights[point] > 0.0F) {
            const float multiplier =
                -data.config.bend_relaxation * weights[point] * coefficients[point] / denominator;
            data.positions[indices[point]] = add3(
                data.positions[indices[point]],
                clamp_length3(
                    mul3(curvature, multiplier),
                    data.config.maximum_position_correction));
        }
    }
}

__device__ void project_contact(DeviceData& data, int32_t vertex) {
    data.contact_flags[vertex] = 0;
    const int32_t face_index = data.candidate_faces[vertex];
    if (face_index < 0 || data.locked[vertex] != 0) {
        return;
    }
    const int3 face = data.body_faces[face_index].vertices;
    const float3 a = data.body_positions[face.x];
    const float3 b = data.body_positions[face.y];
    const float3 c = data.body_positions[face.z];
    const float3 normal = normalized3(cross3(sub3(b, a), sub3(c, a)));
    const float3 closest = closest_triangle_point(data.positions[vertex], a, b, c);
    const float signed_distance = dot3(sub3(data.positions[vertex], closest), normal);
    if (signed_distance < data.config.contact_thickness) {
        const float3 correction = clamp_length3(
            mul3(normal, data.config.contact_thickness - signed_distance),
            data.config.maximum_position_correction * 0.04F);
        data.positions[vertex] = add3(data.positions[vertex], correction);
        data.contact_flags[vertex] = 1;
    }
}

__global__ void set_gravity_kernel(DeviceData* data, float3 gravity) {
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        data->gravity = gravity;
    }
}

__global__ void begin_solve_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex < data->vertex_count) {
        data->click_start[vertex] = data->positions[vertex];
    }
}

__global__ void reset_seam_driven_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex < data->vertex_count) {
        data->seam_driven[vertex] = 0;
    }
}

__global__ void seam_attraction_kernel(DeviceData* data, int32_t begin, int32_t end) {
    const int32_t index = begin + static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index >= end) {
        return;
    }
    Seam& seam = data->seams[index];
    if (seam.captured != 0) {
        return;
    }
    data->seam_driven[seam.a] = 1;
    data->seam_driven[seam.b] = 1;
    const float a_weight = inverse_mass(*data, seam.a);
    const float b_weight = inverse_mass(*data, seam.b);
    const float weight_sum = a_weight + b_weight;
    const float3 difference = sub3(data->positions[seam.b], data->positions[seam.a]);
    const float current_length = length3(difference);
    if (weight_sum > 0.0F && current_length > kEpsilon) {
        const float closure = fminf(data->config.seam_attraction_step, current_length);
        const float3 direction = mul3(difference, 1.0F / current_length);
        data->positions[seam.a] = add3(
            data->positions[seam.a],
            mul3(direction, a_weight / weight_sum * closure));
        data->positions[seam.b] = sub3(
            data->positions[seam.b],
            mul3(direction, b_weight / weight_sum * closure));
    }
}

__global__ void integrate_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex >= data->vertex_count) {
        return;
    }
    data->previous[vertex] = data->positions[vertex];
    if (data->locked[vertex] != 0 || data->inverse_masses[vertex] <= 0.0F) {
        data->velocities[vertex] = make_float3(0.0F, 0.0F, 0.0F);
    } else {
        data->velocities[vertex] = add3(
            data->velocities[vertex],
            mul3(data->gravity, data->config.time_step));
        data->positions[vertex] = add3(
            data->positions[vertex],
            mul3(data->velocities[vertex], data->config.time_step));
    }
}

__global__ void update_seam_capture_kernel(DeviceData* data) {
    const int32_t index = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index >= data->seam_count) {
        return;
    }
    Seam& seam = data->seams[index];
    if (seam.captured == 0) {
        const float3 current = sub3(data->positions[seam.b], data->positions[seam.a]);
        const float3 previous = sub3(data->previous[seam.b], data->previous[seam.a]);
        if (
            length3(current) <= data->config.seam_capture_distance ||
            dot3(current, previous) <= 0.0F) {
            seam.captured = 1;
        }
    }
}

__global__ void project_seam_range_kernel(DeviceData* data, int32_t begin, int32_t end) {
    const int32_t index = begin + static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index < end) {
        const Seam seam = data->seams[index];
        if (seam.captured != 0) {
            project_distance(*data, seam.a, seam.b, 0.0F, 1.0F);
        }
    }
}

__global__ void project_quad_range_kernel(DeviceData* data, int32_t begin, int32_t end) {
    const int32_t index = begin + static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index < end) {
        project_quad(*data, data->quads[index]);
    }
}

__global__ void project_bend_range_kernel(DeviceData* data, int32_t begin, int32_t end) {
    const int32_t index = begin + static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index < end) {
        project_bend(*data, data->bends[index]);
    }
}

__global__ void project_edge_range_kernel(DeviceData* data, int32_t begin, int32_t end) {
    const int32_t index = begin + static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index < end) {
        project_edge(*data, data->edges[index]);
    }
}

__global__ void project_contacts_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex < data->vertex_count) {
        project_contact(*data, vertex);
    }
}

__global__ void finish_substep_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex >= data->vertex_count) {
        return;
    }
    if (data->locked[vertex] != 0 || data->inverse_masses[vertex] <= 0.0F) {
        data->velocities[vertex] = make_float3(0.0F, 0.0F, 0.0F);
    } else if (data->seam_driven[vertex] != 0) {
        data->velocities[vertex] = make_float3(0.0F, 0.0F, 0.0F);
    } else {
        data->velocities[vertex] = mul3(
            sub3(data->positions[vertex], data->previous[vertex]),
            1.0F / data->config.time_step);
        if (data->contact_flags[vertex] != 0) {
            data->velocities[vertex] = mul3(
                data->velocities[vertex],
                data->config.contact_velocity_retention);
        }
    }
}

__global__ void finalize_vertices_kernel(DeviceData* data) {
    const int32_t vertex = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (vertex >= data->vertex_count) {
        return;
    }
    const float3 position = data->positions[vertex];
    const float3 velocity = data->velocities[vertex];
    if (
        !isfinite(position.x) || !isfinite(position.y) || !isfinite(position.z) ||
        !isfinite(velocity.x) || !isfinite(velocity.y) || !isfinite(velocity.z)) {
        atomicExch(&data->stats->nonfinite, 1);
    }
    atomicMax(
        &data->stats->maximum_displacement_bits,
        __float_as_uint(length3(sub3(position, data->click_start[vertex]))));
}

__global__ void finalize_seams_kernel(DeviceData* data) {
    const int32_t index = static_cast<int32_t>(blockIdx.x * blockDim.x + threadIdx.x);
    if (index >= data->seam_count) {
        return;
    }
    const Seam seam = data->seams[index];
    if (seam.captured != 0) {
        atomicAdd(&data->stats->captured_seam_count, 1);
    }
    if (!isfinite(seam.target_length) || seam.target_length < 0.0F) {
        atomicExch(&data->stats->nonfinite, 1);
    }
}

}  // namespace

struct Solver::Impl {
    hsc_config config{};
    int32_t vertex_count = 0;
    int32_t seam_count = 0;
    int32_t edge_count = 0;
    int32_t quad_count = 0;
    int32_t bend_count = 0;
    int32_t body_vertex_count = 0;
    int32_t body_face_count = 0;
    int32_t staging_index = 0;
    cudaStream_t stream = nullptr;
    std::array<cudaEvent_t, 2> staging_events{};
    std::array<float*, 2> staging_body{};
    std::vector<Face> host_body_faces;
    std::vector<int32_t> host_seam_offsets;
    std::vector<int32_t> host_edge_offsets;
    std::vector<int32_t> host_quad_offsets;
    std::vector<int32_t> host_bend_offsets;
    std::vector<int32_t> host_candidate_faces;
    std::map<int32_t, cudaGraphExec_t> solve_graphs;

    DeviceBuffer<float3> positions;
    DeviceBuffer<float3> previous;
    DeviceBuffer<float3> velocities;
    DeviceBuffer<float> inverse_masses;
    DeviceBuffer<int32_t> locked;
    DeviceBuffer<int32_t> seam_driven;
    DeviceBuffer<int32_t> contact_flags;
    DeviceBuffer<int32_t> candidate_faces;
    DeviceBuffer<float3> click_start;
    DeviceBuffer<Seam> seams;
    DeviceBuffer<Edge> edges;
    DeviceBuffer<Quad> quads;
    DeviceBuffer<Bend> bends;
    DeviceBuffer<int32_t> seam_offsets;
    DeviceBuffer<int32_t> edge_offsets;
    DeviceBuffer<int32_t> quad_offsets;
    DeviceBuffer<int32_t> bend_offsets;
    DeviceBuffer<float3> body_positions;
    DeviceBuffer<Face> body_faces;
    DeviceBuffer<BvhNode> bvh_nodes;
    DeviceBuffer<int32_t> bvh_leaf_nodes;
    DeviceBuffer<int32_t> bvh_ready;
    DeviceBuffer<DeviceStats> stats;
    DeviceBuffer<DeviceData> data;

    explicit Impl(const hsc_create_desc& desc, const hsc_config& requested_config)
        : config(requested_config) {
        validate_config();
        validate_descriptor(desc);

        int32_t device_count = 0;
        check_cuda(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount");
        if (device_count <= 0) {
            throw std::runtime_error("No CUDA device is available");
        }
        check_cuda(
            cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking),
            "create CUDA solver stream");

        try {
            initialize(desc);
        } catch (...) {
            if (stream != nullptr) {
                (void)cudaStreamSynchronize(stream);
            }
            release_buffers();
            release_staging();
            if (stream != nullptr) {
                (void)cudaStreamDestroy(stream);
                stream = nullptr;
            }
            throw;
        }
    }

    ~Impl() {
        if (stream != nullptr) {
            (void)cudaStreamSynchronize(stream);
        }
        // CUDA Graph executable and static-runtime teardown can complete work
        // outside the non-blocking stream.  Close the device boundary before
        // graph and allocation destruction; this runs only when a Solver ends.
        (void)cudaDeviceSynchronize();
        for (const auto& [iterations, graph] : solve_graphs) {
            (void)iterations;
            cudaGraphExecDestroy(graph);
        }
        solve_graphs.clear();
        release_buffers();
        release_staging();
        if (stream != nullptr) {
            (void)cudaStreamDestroy(stream);
            stream = nullptr;
        }
    }

    void release_buffers() noexcept {
        data.release();
        stats.release();
        bvh_ready.release();
        bvh_leaf_nodes.release();
        bvh_nodes.release();
        body_faces.release();
        body_positions.release();
        bend_offsets.release();
        quad_offsets.release();
        edge_offsets.release();
        seam_offsets.release();
        bends.release();
        quads.release();
        edges.release();
        seams.release();
        click_start.release();
        candidate_faces.release();
        contact_flags.release();
        seam_driven.release();
        locked.release();
        inverse_masses.release();
        velocities.release();
        previous.release();
        positions.release();
    }

    void release_staging() noexcept {
        for (size_t index = 0; index < staging_events.size(); ++index) {
            if (staging_events[index] != nullptr) {
                cudaEventDestroy(staging_events[index]);
                staging_events[index] = nullptr;
            }
            if (staging_body[index] != nullptr) {
                cudaFreeHost(staging_body[index]);
                staging_body[index] = nullptr;
            }
        }
    }

    void validate_config() const {
        if (
            !(config.time_step > 0.0F) || !std::isfinite(config.time_step) ||
            config.substeps <= 0 || config.iterations <= 0 ||
            !(config.seam_attraction_step > 0.0F) || !std::isfinite(config.seam_attraction_step) ||
            !(config.seam_capture_distance > 0.0F) || !std::isfinite(config.seam_capture_distance) ||
            config.stretch_relaxation < 0.0F || config.stretch_relaxation > 1.0F ||
            !std::isfinite(config.stretch_relaxation) ||
            config.shear_relaxation < 0.0F || config.shear_relaxation > 1.0F ||
            !std::isfinite(config.shear_relaxation) ||
            config.bend_relaxation < 0.0F || config.bend_relaxation > 1.0F ||
            !std::isfinite(config.bend_relaxation) ||
            config.stretch_limit < 0.0F || !std::isfinite(config.stretch_limit) ||
            !(config.maximum_position_correction > 0.0F) ||
            !std::isfinite(config.maximum_position_correction) ||
            !(config.contact_thickness > 0.0F) || !std::isfinite(config.contact_thickness) ||
            config.contact_velocity_retention < 0.0F || config.contact_velocity_retention > 1.0F ||
            !std::isfinite(config.contact_velocity_retention)) {
            throw std::invalid_argument("solver configuration contains an invalid active value");
        }
    }

    static void validate_descriptor(const hsc_create_desc& desc) {
        if (desc.vertex_count <= 0 || desc.positions == nullptr) {
            throw std::invalid_argument("create descriptor has no vertex positions");
        }
        if (desc.seam_count < 0 || (desc.seam_count > 0 && desc.seams == nullptr)) {
            throw std::invalid_argument("create descriptor has invalid seam data");
        }
        if (
            desc.edge_count < 0 ||
            (desc.edge_count > 0 && (desc.edges == nullptr || desc.edge_rest_lengths == nullptr))) {
            throw std::invalid_argument("create descriptor has invalid material edge data");
        }
        if (
            desc.quad_count < 0 ||
            (desc.quad_count > 0 && (desc.quads == nullptr || desc.quad_rest_metrics == nullptr))) {
            throw std::invalid_argument("create descriptor has invalid material quad data");
        }
        if (
            desc.bend_count < 0 ||
            (desc.bend_count > 0 && (desc.bends == nullptr || desc.bend_rest_lengths == nullptr))) {
            throw std::invalid_argument("create descriptor has invalid material bend data");
        }
        if (
            desc.body_vertex_count < 0 || desc.body_face_count < 0 ||
            (desc.body_vertex_count > 0 && desc.body_positions == nullptr) ||
            (desc.body_face_count > 0 && desc.body_faces == nullptr)) {
            throw std::invalid_argument("create descriptor has invalid Body data");
        }
    }

    void initialize(const hsc_create_desc& desc) {
        vertex_count = desc.vertex_count;
        seam_count = desc.seam_count;
        edge_count = desc.edge_count;
        quad_count = desc.quad_count;
        bend_count = desc.bend_count;
        body_vertex_count = desc.body_vertex_count;
        body_face_count = desc.body_face_count;

        std::vector<float3> host_positions(static_cast<size_t>(vertex_count));
        std::vector<float3> host_velocities(static_cast<size_t>(vertex_count));
        std::vector<float> host_inverse_masses(static_cast<size_t>(vertex_count), 1.0F);
        std::vector<int32_t> host_locked(static_cast<size_t>(vertex_count), 0);
        for (int32_t index = 0; index < vertex_count; ++index) {
            if (!finite3(desc.positions + index * 3)) {
                throw std::invalid_argument("create descriptor contains invalid vertex data");
            }
            host_positions[static_cast<size_t>(index)] = host_point(desc.positions, index);
            if (desc.velocities != nullptr) {
                if (!finite3(desc.velocities + index * 3)) {
                    throw std::invalid_argument("create descriptor contains invalid vertex data");
                }
                host_velocities[static_cast<size_t>(index)] = host_point(desc.velocities, index);
            } else {
                host_velocities[static_cast<size_t>(index)] = make_float3(0.0F, 0.0F, 0.0F);
            }
            if (desc.inverse_masses != nullptr) {
                const float value = desc.inverse_masses[index];
                if (!std::isfinite(value) || value < 0.0F) {
                    throw std::invalid_argument("create descriptor contains invalid vertex data");
                }
                host_inverse_masses[static_cast<size_t>(index)] = value;
            }
            if (desc.locked != nullptr && desc.locked[index] != 0) {
                host_locked[static_cast<size_t>(index)] = 1;
                host_velocities[static_cast<size_t>(index)] = make_float3(0.0F, 0.0F, 0.0F);
            }
        }

        std::vector<Seam> host_seams;
        host_seams.reserve(static_cast<size_t>(seam_count));
        for (int32_t index = 0; index < seam_count; ++index) {
            const int32_t a = desc.seams[index * 2];
            const int32_t b = desc.seams[index * 2 + 1];
            validate_index(a, vertex_count, "seam vertex");
            validate_index(b, vertex_count, "seam vertex");
            if (a == b) {
                throw std::invalid_argument("seam endpoints must be distinct");
            }
            const float initial = length3(sub3(
                host_positions[static_cast<size_t>(b)],
                host_positions[static_cast<size_t>(a)]));
            host_seams.push_back({a, b, 0.0F, initial <= config.seam_capture_distance ? 1 : 0});
        }

        std::vector<Edge> host_edges;
        host_edges.reserve(static_cast<size_t>(edge_count));
        for (int32_t index = 0; index < edge_count; ++index) {
            const int32_t a = desc.edges[index * 2];
            const int32_t b = desc.edges[index * 2 + 1];
            const float rest = desc.edge_rest_lengths[index];
            validate_index(a, vertex_count, "material edge vertex");
            validate_index(b, vertex_count, "material edge vertex");
            if (a == b || !(rest > kEpsilon) || !std::isfinite(rest)) {
                throw std::invalid_argument("material edge has invalid rest data");
            }
            host_edges.push_back({a, b, rest});
        }

        std::vector<Quad> host_quads;
        host_quads.reserve(static_cast<size_t>(quad_count));
        for (int32_t index = 0; index < quad_count; ++index) {
            int4 vertices = make_int4(
                desc.quads[index * 4],
                desc.quads[index * 4 + 1],
                desc.quads[index * 4 + 2],
                desc.quads[index * 4 + 3]);
            for (const int32_t vertex : {vertices.x, vertices.y, vertices.z, vertices.w}) {
                validate_index(vertex, vertex_count, "material quad vertex");
            }
            const float uu = desc.quad_rest_metrics[index * 3];
            const float vv = desc.quad_rest_metrics[index * 3 + 1];
            const float uv = desc.quad_rest_metrics[index * 3 + 2];
            if (
                !(uu > kEpsilon * kEpsilon) || !(vv > kEpsilon * kEpsilon) ||
                !std::isfinite(uu) || !std::isfinite(vv) || !std::isfinite(uv)) {
                throw std::invalid_argument("material quad has invalid rest metric");
            }
            host_quads.push_back({vertices, uu, vv, uv});
        }

        std::vector<Bend> host_bends;
        host_bends.reserve(static_cast<size_t>(bend_count));
        for (int32_t index = 0; index < bend_count; ++index) {
            const int3 vertices = make_int3(
                desc.bends[index * 3],
                desc.bends[index * 3 + 1],
                desc.bends[index * 3 + 2]);
            for (const int32_t vertex : {vertices.x, vertices.y, vertices.z}) {
                validate_index(vertex, vertex_count, "material bend vertex");
            }
            const float previous_rest = desc.bend_rest_lengths[index * 2];
            const float next_rest = desc.bend_rest_lengths[index * 2 + 1];
            if (
                !(previous_rest > kEpsilon) || !(next_rest > kEpsilon) ||
                !std::isfinite(previous_rest) || !std::isfinite(next_rest)) {
                throw std::invalid_argument("material bend has invalid rest lengths");
            }
            host_bends.push_back({vertices, previous_rest, next_rest});
        }

        std::vector<float3> host_body_positions(static_cast<size_t>(body_vertex_count));
        for (int32_t index = 0; index < body_vertex_count; ++index) {
            if (!finite3(desc.body_positions + index * 3)) {
                throw std::invalid_argument("Body contains a non-finite vertex");
            }
            host_body_positions[static_cast<size_t>(index)] = host_point(desc.body_positions, index);
        }
        host_body_faces.reserve(static_cast<size_t>(body_face_count));
        for (int32_t index = 0; index < body_face_count; ++index) {
            const int3 face = make_int3(
                desc.body_faces[index * 3],
                desc.body_faces[index * 3 + 1],
                desc.body_faces[index * 3 + 2]);
            for (const int32_t vertex : {face.x, face.y, face.z}) {
                validate_index(vertex, body_vertex_count, "Body face vertex");
            }
            host_body_faces.push_back({face});
        }

        auto [colored_seams, seam_offsets_values] = color_constraints(
            host_seams,
            vertex_count,
            [](const Seam& value) { return std::array<int32_t, 2>{value.a, value.b}; });
        auto [colored_edges, edge_offsets_values] = color_constraints(
            host_edges,
            vertex_count,
            [](const Edge& value) { return std::array<int32_t, 2>{value.a, value.b}; });
        auto [colored_quads, quad_offsets_values] = color_constraints(
            host_quads,
            vertex_count,
            [](const Quad& value) {
                return std::array<int32_t, 4>{
                    value.vertices.x, value.vertices.y, value.vertices.z, value.vertices.w};
            });
        auto [colored_bends, bend_offsets_values] = color_constraints(
            host_bends,
            vertex_count,
            [](const Bend& value) {
                return std::array<int32_t, 3>{
                    value.vertices.x, value.vertices.y, value.vertices.z};
            });

        host_seam_offsets = std::move(seam_offsets_values);
        host_edge_offsets = std::move(edge_offsets_values);
        host_quad_offsets = std::move(quad_offsets_values);
        host_bend_offsets = std::move(bend_offsets_values);
        host_candidate_faces.assign(static_cast<size_t>(vertex_count), -1);

        auto [host_bvh_nodes, host_leaf_nodes] = build_body_bvh(
            host_body_positions, host_body_faces);

        positions.upload(host_positions);
        previous.upload(host_positions);
        velocities.upload(host_velocities);
        inverse_masses.upload(host_inverse_masses);
        locked.upload(host_locked);
        seam_driven.allocate(static_cast<size_t>(vertex_count));
        contact_flags.allocate(static_cast<size_t>(vertex_count));
        candidate_faces.allocate(static_cast<size_t>(vertex_count));
        click_start.allocate(static_cast<size_t>(vertex_count));
        seams.upload(colored_seams);
        edges.upload(colored_edges);
        quads.upload(colored_quads);
        bends.upload(colored_bends);
        seam_offsets.upload(host_seam_offsets);
        edge_offsets.upload(host_edge_offsets);
        quad_offsets.upload(host_quad_offsets);
        bend_offsets.upload(host_bend_offsets);
        body_positions.upload(host_body_positions);
        body_faces.upload(host_body_faces);
        bvh_nodes.upload(host_bvh_nodes);
        bvh_leaf_nodes.upload(host_leaf_nodes);
        bvh_ready.allocate(host_bvh_nodes.size());
        stats.allocate(1);
        check_cuda(cudaMemset(stats.pointer, 0, sizeof(DeviceStats)), "initialize CUDA stats");

        DeviceData host_data{};
        host_data.config = config;
        host_data.gravity = make_float3(0.0F, 0.0F, 0.0F);
        host_data.vertex_count = vertex_count;
        host_data.seam_count = seam_count;
        host_data.edge_count = edge_count;
        host_data.quad_count = quad_count;
        host_data.bend_count = bend_count;
        host_data.body_vertex_count = body_vertex_count;
        host_data.body_face_count = body_face_count;
        host_data.bvh_node_count = static_cast<int32_t>(host_bvh_nodes.size());
        host_data.seam_color_count = static_cast<int32_t>(host_seam_offsets.size()) - 1;
        host_data.edge_color_count = static_cast<int32_t>(host_edge_offsets.size()) - 1;
        host_data.quad_color_count = static_cast<int32_t>(host_quad_offsets.size()) - 1;
        host_data.bend_color_count = static_cast<int32_t>(host_bend_offsets.size()) - 1;
        host_data.positions = positions.pointer;
        host_data.previous = previous.pointer;
        host_data.velocities = velocities.pointer;
        host_data.inverse_masses = inverse_masses.pointer;
        host_data.locked = locked.pointer;
        host_data.seam_driven = seam_driven.pointer;
        host_data.contact_flags = contact_flags.pointer;
        host_data.candidate_faces = candidate_faces.pointer;
        host_data.click_start = click_start.pointer;
        host_data.seams = seams.pointer;
        host_data.edges = edges.pointer;
        host_data.quads = quads.pointer;
        host_data.bends = bends.pointer;
        host_data.seam_color_offsets = seam_offsets.pointer;
        host_data.edge_color_offsets = edge_offsets.pointer;
        host_data.quad_color_offsets = quad_offsets.pointer;
        host_data.bend_color_offsets = bend_offsets.pointer;
        host_data.body_positions = body_positions.pointer;
        host_data.body_faces = body_faces.pointer;
        host_data.bvh_nodes = bvh_nodes.pointer;
        host_data.bvh_leaf_nodes = bvh_leaf_nodes.pointer;
        host_data.bvh_ready = bvh_ready.pointer;
        host_data.stats = stats.pointer;
        data.upload(std::vector<DeviceData>{host_data});
        // DeviceBuffer initialization uses the legacy default stream.  This
        // solver stream is non-blocking, so make the one-time hand-off
        // explicit before any kernel reads the initialized allocations.
        check_cuda(cudaStreamSynchronize(nullptr), "complete CUDA solver uploads");

        if (body_vertex_count > 0) {
            const size_t bytes = static_cast<size_t>(body_vertex_count) * 3 * sizeof(float);
            for (size_t index = 0; index < staging_body.size(); ++index) {
                check_cuda(
                    cudaHostAlloc(reinterpret_cast<void**>(&staging_body[index]), bytes, cudaHostAllocPortable),
                    "cudaHostAlloc Body staging");
                check_cuda(
                    cudaEventCreateWithFlags(&staging_events[index], cudaEventDisableTiming),
                    "cudaEventCreate Body staging");
                check_cuda(cudaEventRecord(staging_events[index], stream), "initialize staging event");
            }
        }

        if (body_face_count > 0) {
            enqueue_bvh_refit();
        }
        check_cuda(cudaStreamSynchronize(stream), "initialize CUDA solver");
    }

    void enqueue_bvh_refit() {
        if (body_face_count <= 0) {
            return;
        }
        check_cuda(
            cudaMemsetAsync(
                bvh_ready.pointer,
                0,
                bvh_ready.count * sizeof(int32_t),
                stream),
            "clear CUDA BVH refit state");
        const int32_t blocks = (body_face_count + kThreads - 1) / kThreads;
        refit_bvh_kernel<<<blocks, kThreads, 0, stream>>>(data.pointer);
        check_cuda(cudaGetLastError(), "launch CUDA BVH refit");
    }

    void synchronize() const {
        check_cuda(cudaStreamSynchronize(stream), "synchronize CUDA solver");
    }

    void synchronize_and_validate() const {
        synchronize();
        DeviceStats host_stats{};
        check_cuda(
            cudaMemcpy(&host_stats, stats.pointer, sizeof(host_stats), cudaMemcpyDeviceToHost),
            "copy CUDA solver status");
        if (host_stats.nonfinite != 0) {
            throw std::runtime_error("CUDA solver state contains a non-finite vertex");
        }
    }

    static int32_t blocks_for(int32_t count) {
        return std::max(1, (count + kThreads - 1) / kThreads);
    }

    template <typename Kernel>
    void launch_range(
        Kernel kernel,
        int32_t begin,
        int32_t end,
        const char* operation) {
        if (end <= begin) {
            return;
        }
        DeviceData* device_data = data.pointer;
        void* arguments[]{&device_data, &begin, &end};
        check_cuda(
            cudaLaunchKernel(
                reinterpret_cast<const void*>(kernel),
                dim3(static_cast<uint32_t>(blocks_for(end - begin))),
                dim3(static_cast<uint32_t>(kThreads)),
                arguments,
                0,
                stream),
            operation);
    }

    void enqueue_standard_solve(int32_t iterations) {
        begin_solve_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
        check_cuda(cudaGetLastError(), "launch CUDA solve initialization");
        for (int32_t substep = 0; substep < config.substeps; ++substep) {
            reset_seam_driven_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
            check_cuda(cudaGetLastError(), "launch CUDA seam reset");
            for (size_t color = 0; color + 1 < host_seam_offsets.size(); ++color) {
                launch_range(
                    seam_attraction_kernel,
                    host_seam_offsets[color],
                    host_seam_offsets[color + 1],
                    "launch CUDA seam attraction");
            }
            integrate_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
            check_cuda(cudaGetLastError(), "launch CUDA integration");

            for (int32_t iteration = 0; iteration < iterations; ++iteration) {
                if (seam_count > 0) {
                    update_seam_capture_kernel<<<blocks_for(seam_count), kThreads, 0, stream>>>(
                        data.pointer);
                    check_cuda(cudaGetLastError(), "launch CUDA seam capture");
                }
                const bool reverse = (iteration & 1) != 0;
                for (size_t pass = 0; pass + 1 < host_seam_offsets.size(); ++pass) {
                    const size_t color = reverse
                        ? host_seam_offsets.size() - 2 - pass
                        : pass;
                    launch_range(
                        project_seam_range_kernel,
                        host_seam_offsets[color],
                        host_seam_offsets[color + 1],
                        "launch CUDA captured seam projection");
                }
                for (size_t pass = 0; pass + 1 < host_quad_offsets.size(); ++pass) {
                    const size_t color = reverse
                        ? host_quad_offsets.size() - 2 - pass
                        : pass;
                    launch_range(
                        project_quad_range_kernel,
                        host_quad_offsets[color],
                        host_quad_offsets[color + 1],
                        "launch CUDA quad projection");
                }
                for (size_t pass = 0; pass + 1 < host_bend_offsets.size(); ++pass) {
                    const size_t color = reverse
                        ? host_bend_offsets.size() - 2 - pass
                        : pass;
                    launch_range(
                        project_bend_range_kernel,
                        host_bend_offsets[color],
                        host_bend_offsets[color + 1],
                        "launch CUDA bend projection");
                }
                for (int32_t sweep = 0; sweep < 4; ++sweep) {
                    const bool sweep_reverse = ((sweep & 1) != 0) != reverse;
                    for (size_t pass = 0; pass + 1 < host_edge_offsets.size(); ++pass) {
                        const size_t color = sweep_reverse
                            ? host_edge_offsets.size() - 2 - pass
                            : pass;
                        launch_range(
                            project_edge_range_kernel,
                            host_edge_offsets[color],
                            host_edge_offsets[color + 1],
                            "launch CUDA edge projection");
                    }
                }
                project_contacts_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
                check_cuda(cudaGetLastError(), "launch CUDA contact projection");
            }
            finish_substep_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
            check_cuda(cudaGetLastError(), "launch CUDA substep completion");
        }
        finalize_vertices_kernel<<<blocks_for(vertex_count), kThreads, 0, stream>>>(data.pointer);
        check_cuda(cudaGetLastError(), "launch CUDA vertex validation");
        if (seam_count > 0) {
            finalize_seams_kernel<<<blocks_for(seam_count), kThreads, 0, stream>>>(data.pointer);
            check_cuda(cudaGetLastError(), "launch CUDA seam validation");
        }
    }

    cudaGraphExec_t graph_for_iterations(int32_t iterations) {
        const auto existing = solve_graphs.find(iterations);
        if (existing != solve_graphs.end()) {
            return existing->second;
        }
        synchronize_and_validate();
        check_cuda(
            cudaStreamBeginCapture(stream, cudaStreamCaptureModeThreadLocal),
            "begin CUDA solver graph capture");
        bool capture_active = true;
        cudaGraph_t graph = nullptr;
        cudaGraphExec_t executable = nullptr;
        try {
            enqueue_standard_solve(iterations);
            check_cuda(cudaStreamEndCapture(stream, &graph), "end CUDA solver graph capture");
            capture_active = false;
            check_cuda(cudaGraphInstantiate(&executable, graph, 0), "instantiate CUDA solver graph");
            check_cuda(cudaGraphDestroy(graph), "destroy CUDA solver capture graph");
            graph = nullptr;
            check_cuda(cudaGraphUpload(executable, stream), "upload CUDA solver graph");
            check_cuda(cudaStreamSynchronize(stream), "prepare CUDA solver graph");
            solve_graphs.emplace(iterations, executable);
            return executable;
        } catch (...) {
            if (capture_active) {
                cudaGraph_t abandoned = nullptr;
                (void)cudaStreamEndCapture(stream, &abandoned);
                if (abandoned != nullptr) {
                    (void)cudaGraphDestroy(abandoned);
                }
            }
            if (graph != nullptr) {
                (void)cudaGraphDestroy(graph);
            }
            if (executable != nullptr) {
                (void)cudaGraphExecDestroy(executable);
            }
            throw;
        }
    }

    void enqueue_advance(
        const float gravity_values[3],
        int32_t requested_iterations,
        const int32_t* explicit_candidates,
        int32_t explicit_candidate_count,
        bool automatic_candidates) {
        if (
            gravity_values == nullptr || !finite3(gravity_values) ||
            requested_iterations < 0) {
            throw std::invalid_argument("advance descriptor contains an invalid value");
        }
        int32_t iterations = requested_iterations > 0 ? requested_iterations : config.iterations;
        if (iterations <= 0 || iterations > 128) {
            throw std::invalid_argument("advance iteration count is out of range");
        }
        const cudaGraphExec_t solve_graph = graph_for_iterations(iterations);

        if (!automatic_candidates) {
            if (explicit_candidate_count < 0) {
                throw std::invalid_argument("advance descriptor contains an invalid candidate count");
            }
            if (explicit_candidate_count > 0 && explicit_candidates == nullptr) {
                throw std::invalid_argument("advance descriptor has no Body candidates");
            }
            std::fill(host_candidate_faces.begin(), host_candidate_faces.end(), -1);
            for (int32_t index = 0; index < explicit_candidate_count; ++index) {
                const int32_t vertex = explicit_candidates[index * 2];
                const int32_t face = explicit_candidates[index * 2 + 1];
                validate_index(vertex, vertex_count, "Body candidate vertex");
                validate_index(face, body_face_count, "Body candidate face");
                host_candidate_faces[static_cast<size_t>(vertex)] = face;
            }
            synchronize_and_validate();
            check_cuda(
                cudaMemcpyAsync(
                    candidate_faces.pointer,
                    host_candidate_faces.data(),
                    host_candidate_faces.size() * sizeof(int32_t),
                    cudaMemcpyHostToDevice,
                    stream),
                "upload explicit Body candidates");
        }

        initialize_stats_kernel<<<1, 1, 0, stream>>>(
            data.pointer,
            iterations,
            automatic_candidates ? 0 : explicit_candidate_count);
        check_cuda(cudaGetLastError(), "launch CUDA stats initialization");
        const float3 gravity = make_float3(
            gravity_values[0], gravity_values[1], gravity_values[2]);
        set_gravity_kernel<<<1, 1, 0, stream>>>(data.pointer, gravity);
        check_cuda(cudaGetLastError(), "launch CUDA gravity update");
        if (automatic_candidates) {
            const int32_t blocks = (vertex_count + kThreads - 1) / kThreads;
            find_body_candidates_kernel<<<blocks, kThreads, 0, stream>>>(data.pointer);
            check_cuda(cudaGetLastError(), "launch CUDA Body candidate search");
        }

        check_cuda(cudaGraphLaunch(solve_graph, stream), "launch CUDA cloth solver graph");
    }

    hsc_stats copy_stats() const {
        synchronize_and_validate();
        DeviceStats source{};
        check_cuda(
            cudaMemcpy(&source, stats.pointer, sizeof(source), cudaMemcpyDeviceToHost),
            "copy CUDA solver statistics");
        hsc_stats result{};
        result.substeps = source.substeps;
        result.iterations = source.iterations;
        result.seam_count = source.seam_count;
        result.captured_seam_count = source.captured_seam_count;
        result.edge_count = source.edge_count;
        result.quad_count = source.quad_count;
        result.bend_count = source.bend_count;
        result.body_candidate_count = source.body_candidate_count;
        std::memcpy(&result.maximum_displacement, &source.maximum_displacement_bits, sizeof(float));
        return result;
    }
};

hsc_config default_config() {
    hsc_config config{};
    config.time_step = 1.0F / 240.0F;
    config.substeps = 8;
    config.iterations = 16;
    config.seam_attraction_step = 0.008F;
    config.seam_capture_distance = 0.002F;
    config.stretch_relaxation = 1.0F;
    config.shear_relaxation = 0.02F;
    config.bend_relaxation = 0.02F;
    config.stretch_limit = 0.05F;
    config.maximum_position_correction = 0.005F;
    config.contact_thickness = 0.01F;
    config.contact_velocity_retention = 0.0F;
    return config;
}

Solver::Solver(const hsc_create_desc& desc, const hsc_config& config)
    : impl_(std::make_unique<Impl>(desc, config)) {}

Solver::~Solver() = default;

int32_t Solver::vertex_count() const noexcept {
    return impl_->vertex_count;
}

int32_t Solver::seam_count() const noexcept {
    return impl_->seam_count;
}

void Solver::replace_state(
    const float* positions_values,
    const float* velocity_values,
    const int32_t* locked_values) {
    if (positions_values == nullptr || velocity_values == nullptr || locked_values == nullptr) {
        throw std::invalid_argument("replacement state pointer is null");
    }
    for (int32_t index = 0; index < impl_->vertex_count; ++index) {
        if (!finite3(positions_values + index * 3) || !finite3(velocity_values + index * 3)) {
            throw std::invalid_argument("replacement state contains a non-finite vertex");
        }
    }
    impl_->synchronize();
    const size_t vector_bytes = static_cast<size_t>(impl_->vertex_count) * sizeof(float3);
    check_cuda(
        cudaMemcpyAsync(
            impl_->positions.pointer,
            positions_values,
            vector_bytes,
            cudaMemcpyHostToDevice,
            impl_->stream),
        "replace CUDA positions");
    check_cuda(
        cudaMemcpyAsync(
            impl_->previous.pointer,
            positions_values,
            vector_bytes,
            cudaMemcpyHostToDevice,
            impl_->stream),
        "replace CUDA previous positions");
    check_cuda(
        cudaMemcpyAsync(
            impl_->velocities.pointer,
            velocity_values,
            vector_bytes,
            cudaMemcpyHostToDevice,
            impl_->stream),
        "replace CUDA velocities");
    std::vector<int32_t> normalized_locked(static_cast<size_t>(impl_->vertex_count));
    for (int32_t index = 0; index < impl_->vertex_count; ++index) {
        normalized_locked[static_cast<size_t>(index)] = locked_values[index] != 0 ? 1 : 0;
    }
    check_cuda(
        cudaMemcpyAsync(
            impl_->locked.pointer,
            normalized_locked.data(),
            normalized_locked.size() * sizeof(int32_t),
            cudaMemcpyHostToDevice,
            impl_->stream),
        "replace CUDA locked flags");
    check_cuda(
        cudaMemsetAsync(impl_->stats.pointer, 0, sizeof(DeviceStats), impl_->stream),
        "clear CUDA error state");
    // The caller owns these pageable arrays, including normalized_locked.
    // Complete the rollback upload before their lifetime ends.
    impl_->synchronize();
}

void Solver::copy_state(float* positions_values, float* velocity_values) const {
    if (positions_values == nullptr || velocity_values == nullptr) {
        throw std::invalid_argument("state output pointer is null");
    }
    impl_->synchronize_and_validate();
    const size_t vector_bytes = static_cast<size_t>(impl_->vertex_count) * sizeof(float3);
    check_cuda(
        cudaMemcpy(
            positions_values, impl_->positions.pointer, vector_bytes, cudaMemcpyDeviceToHost),
        "copy CUDA positions");
    check_cuda(
        cudaMemcpy(
            velocity_values, impl_->velocities.pointer, vector_bytes, cudaMemcpyDeviceToHost),
        "copy CUDA velocities");
}

void Solver::replace_body(
    int32_t vertex_count,
    const float* positions_values,
    int32_t face_count,
    const int32_t* faces_values) {
    if (vertex_count != impl_->body_vertex_count || face_count != impl_->body_face_count) {
        throw std::invalid_argument("replacement Body topology count changed");
    }
    if ((vertex_count > 0 && positions_values == nullptr) || (face_count > 0 && faces_values == nullptr)) {
        throw std::invalid_argument("replacement Body pointer is null");
    }
    for (int32_t index = 0; index < vertex_count; ++index) {
        if (!finite3(positions_values + index * 3)) {
            throw std::invalid_argument("replacement Body contains a non-finite vertex");
        }
    }
    std::vector<Face> replacement_faces;
    replacement_faces.reserve(static_cast<size_t>(face_count));
    for (int32_t index = 0; index < face_count; ++index) {
        const int3 face = make_int3(
            faces_values[index * 3], faces_values[index * 3 + 1], faces_values[index * 3 + 2]);
        for (const int32_t vertex : {face.x, face.y, face.z}) {
            validate_index(vertex, vertex_count, "replacement Body face vertex");
        }
        replacement_faces.push_back({face});
    }

    const bool faces_changed = replacement_faces.size() != impl_->host_body_faces.size() ||
        !std::equal(
            replacement_faces.begin(),
            replacement_faces.end(),
            impl_->host_body_faces.begin(),
            [](const Face& left, const Face& right) {
                return left.vertices.x == right.vertices.x &&
                    left.vertices.y == right.vertices.y &&
                    left.vertices.z == right.vertices.z;
            });
    if (faces_changed) {
        impl_->synchronize_and_validate();
        check_cuda(
            cudaMemcpyAsync(
                impl_->body_faces.pointer,
                replacement_faces.data(),
                replacement_faces.size() * sizeof(Face),
                cudaMemcpyHostToDevice,
                impl_->stream),
            "replace CUDA Body faces");
        impl_->synchronize();
        impl_->host_body_faces = replacement_faces;
    }

    if (vertex_count > 0) {
        const int32_t slot = impl_->staging_index;
        impl_->staging_index = (impl_->staging_index + 1) % 2;
        check_cuda(
            cudaEventSynchronize(impl_->staging_events[static_cast<size_t>(slot)]),
            "wait for CUDA Body staging buffer");
        const size_t bytes = static_cast<size_t>(vertex_count) * 3 * sizeof(float);
        std::memcpy(impl_->staging_body[static_cast<size_t>(slot)], positions_values, bytes);
        check_cuda(
            cudaMemcpyAsync(
                impl_->body_positions.pointer,
                impl_->staging_body[static_cast<size_t>(slot)],
                bytes,
                cudaMemcpyHostToDevice,
                impl_->stream),
            "upload CUDA Body pose");
        check_cuda(
            cudaEventRecord(
                impl_->staging_events[static_cast<size_t>(slot)], impl_->stream),
            "record CUDA Body upload");
        impl_->enqueue_bvh_refit();
    }
}

void Solver::replace_seam_state(const float* target_lengths) {
    if (target_lengths == nullptr && impl_->seam_count > 0) {
        throw std::invalid_argument("seam state input pointer is null");
    }
    for (int32_t index = 0; index < impl_->seam_count; ++index) {
        if (!std::isfinite(target_lengths[index]) || std::abs(target_lengths[index]) > kEpsilon) {
            throw std::invalid_argument("seam targets are fixed at zero length");
        }
    }
}

void Solver::copy_seam_state(float* target_lengths) const {
    if (target_lengths == nullptr && impl_->seam_count > 0) {
        throw std::invalid_argument("seam state output pointer is null");
    }
    impl_->synchronize_and_validate();
    std::fill(target_lengths, target_lengths + impl_->seam_count, 0.0F);
}

hsc_stats Solver::advance(const hsc_advance_desc& desc) {
    impl_->enqueue_advance(
        desc.gravity,
        desc.iterations,
        desc.body_candidates,
        desc.body_candidate_count,
        false);
    return impl_->copy_stats();
}

void Solver::advance_resident(const float gravity[3], int32_t iterations) {
    impl_->enqueue_advance(gravity, iterations, nullptr, 0, true);
}

}  // namespace hsc
