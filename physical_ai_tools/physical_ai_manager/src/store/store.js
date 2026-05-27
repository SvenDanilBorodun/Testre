/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Author: Kiwoong Park
 */

import { configureStore } from '@reduxjs/toolkit';
import taskSlice from '../features/tasks/taskSlice';
import uiSlice from '../features/ui/uiSlice';
import rosSlice from '../features/ros/rosSlice';
import trainingSlice from '../features/training/trainingSlice';
import editDatasetSlice from '../features/editDataset/editDatasetSlice';
import authSlice from '../features/auth/authSlice';
import teacherSlice from '../features/teacher/teacherSlice';
import adminSlice from '../features/admin/adminSlice';
import workshopSlice from '../features/workshop/workshopSlice';
import jetsonSlice from './jetsonSlice';

export const store = configureStore({
  reducer: {
    tasks: taskSlice,
    ros: rosSlice,
    ui: uiSlice,
    training: trainingSlice,
    editDataset: editDatasetSlice,
    auth: authSlice,
    teacher: teacherSlice,
    admin: adminSlice,
    workshop: workshopSlice,
    jetson: jetsonSlice,
  },
  middleware: (getDefaultMiddleware) => getDefaultMiddleware(),
});

export default store;
