#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
# Modifications copyright 2026 Sven Danil Borodun (EduBotics)
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
#
# Author: Seongwoo Kim
#
# Modified 2026-05 for EduBotics v2.5.0.
#
# On-device training was removed; only the two static helpers in
# training_manager.py remain (used by inference-side callbacks). Modal Cloud
# does all real training. See CLAUDE.md "Six non-negotiable rules" §5.
