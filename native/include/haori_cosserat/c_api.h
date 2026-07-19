// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <stdint.h>

#if defined(_WIN32)
#  if defined(HSC_BUILD_DLL)
#    define HSC_API __declspec(dllexport)
#  else
#    define HSC_API __declspec(dllimport)
#  endif
#else
#  define HSC_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define HSC_API_VERSION 10

typedef void* hsc_handle;

typedef enum hsc_status {
    HSC_STATUS_OK = 0,
    HSC_STATUS_INVALID_ARGUMENT = 1,
    HSC_STATUS_OUT_OF_RANGE = 2,
    HSC_STATUS_NONFINITE_STATE = 3,
    HSC_STATUS_INTERNAL_ERROR = 4
} hsc_status;

typedef struct hsc_config {
    float time_step;
    int32_t substeps;
    int32_t iterations;
    /* Per-iteration closure distance for an uncaptured seam pair, in metres.
       Constant, so pair distance does not change the closing rate. */
    float seam_attraction_step;
    float seam_capture_distance;
    /* Per-iteration material energy-projection fractions in [0, 1]. */
    float stretch_relaxation;
    float shear_relaxation;
    float bend_relaxation;
    /* Rest-length fraction a warp/weft span may gain before it becomes a hard
       wall.  This is the weave's crimp reserve, not yarn elongation. */
    float stretch_limit;
    float maximum_position_correction;
    float contact_thickness;
    /* Velocity fraction a Body-contacting vertex keeps across a substep.  Zero
       dissipates all kinetic energy at the contact, so Body motion can never
       accelerate the cloth. */
    float contact_velocity_retention;
} hsc_config;

typedef struct hsc_create_desc {
    int32_t vertex_count;
    const float* positions;
    const float* velocities;
    const float* inverse_masses;
    const int32_t* locked;

    int32_t seam_count;
    const int32_t* seams;

    int32_t edge_count;
    const int32_t* edges;
    const float* edge_rest_lengths;

    int32_t quad_count;
    const int32_t* quads;
    /* Per quad: rest dot(u,u), dot(v,v), dot(u,v). */
    const float* quad_rest_metrics;

    int32_t bend_count;
    /* Per bend: previous, center, next vertex along one material axis. */
    const int32_t* bends;
    /* Per bend: the two positive rest segment lengths. */
    const float* bend_rest_lengths;

    int32_t body_vertex_count;
    const float* body_positions;
    int32_t body_face_count;
    const int32_t* body_faces;
} hsc_create_desc;

typedef struct hsc_advance_desc {
    float gravity[3];
    int32_t iterations;
    int32_t body_candidate_count;
    const int32_t* body_candidates;
} hsc_advance_desc;

typedef struct hsc_stats {
    int32_t substeps;
    int32_t iterations;
    int32_t seam_count;
    int32_t captured_seam_count;
    int32_t edge_count;
    int32_t quad_count;
    int32_t bend_count;
    int32_t body_candidate_count;
    float maximum_displacement;
} hsc_stats;

HSC_API int32_t hsc_get_api_version(void);
HSC_API hsc_status hsc_default_config(hsc_config* out_config);

HSC_API hsc_status hsc_create(
    const hsc_create_desc* desc,
    const hsc_config* config,
    hsc_handle* out_handle,
    char* error_message,
    int32_t error_capacity);

HSC_API void hsc_destroy(hsc_handle handle);

HSC_API hsc_status hsc_get_counts(
    hsc_handle handle,
    int32_t* vertex_count,
    int32_t* seam_count,
    char* error_message,
    int32_t error_capacity);

HSC_API hsc_status hsc_replace_state(
    hsc_handle handle,
    const float* positions,
    const float* velocities,
    const int32_t* locked,
    char* error_message,
    int32_t error_capacity);

HSC_API hsc_status hsc_copy_state(
    hsc_handle handle,
    float* positions,
    float* velocities,
    char* error_message,
    int32_t error_capacity);

/* Replace the evaluated Body pose without rebuilding the cloth solver.  The
   vertex and triangle counts must stay constant, as they do for an Armature
   deformation. */
HSC_API hsc_status hsc_replace_body(
    hsc_handle handle,
    int32_t body_vertex_count,
    const float* body_positions,
    int32_t body_face_count,
    const int32_t* body_faces,
    char* error_message,
    int32_t error_capacity);

HSC_API hsc_status hsc_replace_seam_state(
    hsc_handle handle,
    const float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity);

HSC_API hsc_status hsc_copy_seam_state(
    hsc_handle handle,
    float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity);

HSC_API hsc_status hsc_advance(
    hsc_handle handle,
    const hsc_advance_desc* desc,
    hsc_stats* out_stats,
    char* error_message,
    int32_t error_capacity);

/* Enqueue one complete Body step using GPU-generated Body candidates.  Cloth
   positions, velocities, constraints, and collision state remain resident on
   the CUDA device.  After the graph has been created, the call only enqueues
   work.  hsc_copy_state and hsc_advance synchronize and report deferred CUDA
   failures; hsc_destroy synchronizes before releasing device memory. */
HSC_API hsc_status hsc_advance_resident(
    hsc_handle handle,
    const float gravity[3],
    int32_t iterations,
    char* error_message,
    int32_t error_capacity);

#ifdef __cplusplus
}
#endif
