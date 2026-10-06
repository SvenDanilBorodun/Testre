#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
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

"""Daten 2.0: the robot side of the student's „Daten" tab.

``contract``      the wire constants, mirrored by the page (datenContract.js)
``link_tokens``   short-lived HMAC link tokens for the read-only media routes
``texts_de``      every German sentence of the Daten service and its workers
``library``       the local dataset library, summaries, clips, hints (sidecar)
``hub_reads``     the sidecar's bounded, cached Hugging Face reads
``http_server``   the read-only sidecar process (port 8095, never imports ROS)
``node_service``  the node's /daten/command service, leases, jobs, recovery
``download_worker`` the ONE dataset download process (its own token watch)

Nothing here is imported at package import time: the sidecar and the workers
are separate processes, and the node imports ``node_service`` itself.
"""
