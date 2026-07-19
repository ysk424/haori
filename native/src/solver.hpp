// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "haori_cosserat/c_api.h"

#include <cstdint>
#include <memory>

namespace hsc {

class Solver {
public:
    Solver(const hsc_create_desc& desc, const hsc_config& config);
    ~Solver();

    Solver(const Solver&) = delete;
    Solver& operator=(const Solver&) = delete;
    Solver(Solver&&) = delete;
    Solver& operator=(Solver&&) = delete;

    [[nodiscard]] int32_t vertex_count() const noexcept;
    [[nodiscard]] int32_t seam_count() const noexcept;

    void replace_state(
        const float* positions,
        const float* velocities,
        const int32_t* locked);
    void copy_state(float* positions, float* velocities) const;

    void replace_body(
        int32_t vertex_count,
        const float* positions,
        int32_t face_count,
        const int32_t* faces);

    void replace_seam_state(const float* target_lengths);
    void copy_seam_state(float* target_lengths) const;

    hsc_stats advance(const hsc_advance_desc& desc);
    void advance_resident(const float gravity[3], int32_t iterations);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

hsc_config default_config();

}  // namespace hsc
