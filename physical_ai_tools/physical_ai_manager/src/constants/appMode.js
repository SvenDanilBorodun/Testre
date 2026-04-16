// App mode determines which UI branch renders.
// Set at build time via REACT_APP_MODE env var.
//   - 'student' (default): Docker build that runs locally with the robot.
//     Students log in via the Cloud tab; teachers/admins are rejected.
//   - 'web': public web deployment for teachers and admin dashboards.
//     Students are rejected ("use the desktop app").
export const APP_MODE = process.env.REACT_APP_MODE === 'web' ? 'web' : 'student';

export const SYNTHETIC_EMAIL_DOMAIN = 'edubotics.local';

export const usernameToEmail = (username) =>
  `${String(username).trim().toLowerCase()}@${SYNTHETIC_EMAIL_DOMAIN}`;
