// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The password rule of the teacher web (owner decision 2026-11-10): a NEW or
// RESET password is at least 12 characters for a teacher or an admin and at
// least 8 for a student, at most 128. The cloud API refuses anything else in
// German (cloud_training_api/app/services/passwords.py); the forms refuse first,
// so a teacher reads the rule before submitting. Existing passwords keep
// working, which is why the LOGIN form still accepts shorter ones.
// robotis_ai_setup/tests/test_password_rule_lockstep.py holds the numbers equal.

export const STAFF_PASSWORD_MIN = 12;
export const STUDENT_PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;

const RULES = {
  staff: { min: STAFF_PASSWORD_MIN, who: 'Lehrkräfte und Admins' },
  student: { min: STUDENT_PASSWORD_MIN, who: 'Schülerinnen und Schüler' },
};

function ruleOf(audience) {
  const rule = RULES[audience];
  if (!rule) throw new Error(`unknown password audience: ${audience}`);
  return rule;
}

export function passwordMinLength(audience) {
  return ruleOf(audience).min;
}

/** The hint under a password field, e.g. „Mindestens 12 Zeichen (höchstens 128)." */
export function passwordHintDe(audience) {
  return `Mindestens ${ruleOf(audience).min} Zeichen (höchstens ${PASSWORD_MAX}).`;
}

/** The German refusal of `password`, or null when it follows the rule. Same
 *  wording as the API's `passwords.password_error_de`. */
export function passwordErrorDe(password, audience) {
  const rule = ruleOf(audience);
  // Code points, like Python's len() on the server (an emoji is one character).
  const length = typeof password === 'string' ? Array.from(password).length : 0;
  if (length < rule.min) {
    return `Das Passwort muss für ${rule.who} mindestens ${rule.min} Zeichen lang sein.`;
  }
  if (length > PASSWORD_MAX) {
    return `Das Passwort darf höchstens ${PASSWORD_MAX} Zeichen lang sein.`;
  }
  return null;
}
