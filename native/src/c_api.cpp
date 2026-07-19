// SPDX-License-Identifier: GPL-3.0-or-later
#include "solver.hpp"

#include <algorithm>
#include <cstring>
#include <exception>
#include <new>
#include <stdexcept>
#include <string>

namespace {

void write_error(char* output, int32_t capacity, const std::string& message) noexcept {
    if (output == nullptr || capacity <= 0) {
        return;
    }
    const size_t available = static_cast<size_t>(capacity - 1);
    const size_t count = std::min(available, message.size());
    std::memcpy(output, message.data(), count);
    output[count] = '\0';
}

void clear_error(char* output, int32_t capacity) noexcept {
    if (output != nullptr && capacity > 0) {
        output[0] = '\0';
    }
}

hsc_status classify_exception(const std::exception& exception) noexcept {
    if (dynamic_cast<const std::invalid_argument*>(&exception) != nullptr) {
        return HSC_STATUS_INVALID_ARGUMENT;
    }
    if (dynamic_cast<const std::out_of_range*>(&exception) != nullptr) {
        return HSC_STATUS_OUT_OF_RANGE;
    }
    const std::string message = exception.what();
    if (message.find("non-finite") != std::string::npos) {
        return HSC_STATUS_NONFINITE_STATE;
    }
    return HSC_STATUS_INTERNAL_ERROR;
}

hsc::Solver& require_solver(hsc_handle handle) {
    if (handle == nullptr) {
        throw std::invalid_argument("solver handle is null");
    }
    return *static_cast<hsc::Solver*>(handle);
}

template <typename Function>
hsc_status guard(char* error_message, int32_t error_capacity, Function&& function) noexcept {
    clear_error(error_message, error_capacity);
    try {
        function();
        return HSC_STATUS_OK;
    } catch (const std::exception& exception) {
        write_error(error_message, error_capacity, exception.what());
        return classify_exception(exception);
    } catch (...) {
        write_error(error_message, error_capacity, "unknown native solver failure");
        return HSC_STATUS_INTERNAL_ERROR;
    }
}

}  // namespace

extern "C" {

int32_t hsc_get_api_version(void) {
    return HSC_API_VERSION;
}

hsc_status hsc_default_config(hsc_config* out_config) {
    if (out_config == nullptr) {
        return HSC_STATUS_INVALID_ARGUMENT;
    }
    *out_config = hsc::default_config();
    return HSC_STATUS_OK;
}

hsc_status hsc_create(
    const hsc_create_desc* desc,
    const hsc_config* config,
    hsc_handle* out_handle,
    char* error_message,
    int32_t error_capacity) {
    if (out_handle != nullptr) {
        *out_handle = nullptr;
    }
    return guard(error_message, error_capacity, [&]() {
        if (desc == nullptr || config == nullptr || out_handle == nullptr) {
            throw std::invalid_argument("hsc_create received a null argument");
        }
        *out_handle = static_cast<hsc_handle>(new hsc::Solver(*desc, *config));
    });
}

void hsc_destroy(hsc_handle handle) {
    delete static_cast<hsc::Solver*>(handle);
}

hsc_status hsc_get_counts(
    hsc_handle handle,
    int32_t* vertex_count,
    int32_t* seam_count,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        if (vertex_count == nullptr || seam_count == nullptr) {
            throw std::invalid_argument("count output pointer is null");
        }
        hsc::Solver& solver = require_solver(handle);
        *vertex_count = solver.vertex_count();
        *seam_count = solver.seam_count();
    });
}

hsc_status hsc_replace_state(
    hsc_handle handle,
    const float* positions,
    const float* velocities,
    const int32_t* locked,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).replace_state(positions, velocities, locked);
    });
}

hsc_status hsc_copy_state(
    hsc_handle handle,
    float* positions,
    float* velocities,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).copy_state(positions, velocities);
    });
}

hsc_status hsc_replace_body(
    hsc_handle handle,
    int32_t body_vertex_count,
    const float* body_positions,
    int32_t body_face_count,
    const int32_t* body_faces,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).replace_body(
            body_vertex_count,
            body_positions,
            body_face_count,
            body_faces);
    });
}

hsc_status hsc_replace_seam_state(
    hsc_handle handle,
    const float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).replace_seam_state(seam_target_lengths);
    });
}

hsc_status hsc_copy_seam_state(
    hsc_handle handle,
    float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).copy_seam_state(seam_target_lengths);
    });
}

hsc_status hsc_advance(
    hsc_handle handle,
    const hsc_advance_desc* desc,
    hsc_stats* out_stats,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        if (desc == nullptr || out_stats == nullptr) {
            throw std::invalid_argument("advance descriptor or output is null");
        }
        *out_stats = require_solver(handle).advance(*desc);
    });
}

hsc_status hsc_advance_resident(
    hsc_handle handle,
    const float gravity[3],
    int32_t iterations,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        if (gravity == nullptr) {
            throw std::invalid_argument("resident advance gravity is null");
        }
        require_solver(handle).advance_resident(gravity, iterations);
    });
}

}  // extern "C"
