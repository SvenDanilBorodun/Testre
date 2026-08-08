// The side-effect half of utils/bootScrub, split out so the decision itself
// stays a plain testable function.
//
// It has to be a side-effect IMPORT rather than a call in index.js's body,
// because ES module imports are hoisted and evaluated before any statement of
// the importing module runs. The two things the scrub must beat both read
// localStorage at MODULE EVALUATION time:
//
//   * store/store.js -> features/tasks/taskSlice + features/training/
//     trainingSlice, whose `initialState`s ARE localStorage snapshots
//     (`edubotics_userId`, `edubotics_trainingInfo`);
//   * lib/supabaseClient.js, which calls `supabase.auth.getSession()` at import
//     to restore the persisted session and push its JWT to Realtime.
//
// So this module must be evaluated FIRST, which is why index.js imports it
// above everything else and `bootScrub.test.js` pins that position.
import { bootScrubOnce } from './bootScrub';

bootScrubOnce();
