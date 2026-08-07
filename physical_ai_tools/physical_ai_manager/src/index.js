// FIRST, and it has to stay first. Student handover on a shared Windows
// account: a freshly spawned window carries `?fresh=<nonce>` and this import
// scrubs the previous student's identity out of browser storage. Both of the
// imports below it read localStorage while they are being EVALUATED —
// `./store/store` through taskSlice/trainingSlice's initialState, and (via
// `./App`) lib/supabaseClient through `auth.getSession()` — so a scrub placed
// anywhere after them would run against state that had already been handed to
// Redux and to supabase-js. Import order is the whole mechanism; a call in this
// file's BODY would be too late, because imports are hoisted.
import './utils/bootScrubOnLoad';
import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import reportWebVitals from './reportWebVitals';
import { Provider } from 'react-redux';
import store from './store/store';

// NO StrictMode. React 19 StrictMode double-mounts every effect in DEV builds
// (production is unaffected either way), and the rosbridge subscription
// lifecycle in useRosTopicSubscription is not double-mount-safe: the second
// mount's async initializeSubscriptions races the first one's cleanup and the
// /task/status subscription ends up dead — live UI updates (incl. the
// collision-stop modal) silently stop while service calls still work.
// Confirmed on-rig 2026-06-04 with `npm start`: a 5 Hz phase=COLLISION stream
// reached a raw websocket probe but never the app. The shipped nginx build
// never had the bug (StrictMode is a dev-only behavior), so removing the
// wrapper aligns dev with what students actually run.
const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <Provider store={store}>
    <App />
  </Provider>
);

// If you want to start measuring performance in your app, pass a function
// to log results (for example: reportWebVitals(console.log))
// or send to an analytics endpoint. Learn more: https://bit.ly/CRA-vitals
reportWebVitals();
