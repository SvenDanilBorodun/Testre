/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * The object catalog the SERVER ships
 * (`workflow/object_catalog.py::_FIXED_CATALOG`), for the two surfaces that
 * cannot ask a robot for it: the simulator before a rig has answered, and the
 * teacher-web template editor, which has no rosbridge at all — every „Greife"
 * block in a published template used to carry the „(lädt …)" placeholder as its
 * object type.
 *
 * Its own module on purpose: `blocks/perception.js` holds live editor state
 * (the delivered catalog, the workspace accessor) and the page tests mock it,
 * so a constant living there could not be read by the page that needs it.
 */
export const DEFAULT_OBJECT_CATALOG = [['Würfel', 'wuerfel']];

export default DEFAULT_OBJECT_CATALOG;
