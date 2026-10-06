// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Daten tab's toasts (spec §G8): react-hot-toast (aria-live), capped at
// three by the page, with an action button where the mockup has one
// („Jetzt hochladen", „Neuen ansehen", „Ansehen"). Every text comes from
// datenCopy or is the robot's own German sentence.

import React from 'react';
import toast from 'react-hot-toast';
import Icon from '../icons/Icon';
import { toastIcon } from '../icons/toast';
import { releasePointerFocus } from '../Record/ActionBar';

function Body({ t, text, action }) {
  return (
    <span className="dat-toast-body">
      <span>{text}</span>
      <button
        type="button"
        onClick={(e) => {
          releasePointerFocus(e);
          toast.dismiss(t.id);
          action.onClick();
        }}
      >
        {action.label}
      </button>
    </span>
  );
}

/** A green toast; with `action` ({label, onClick}) it carries a button and stays 8 s. */
export function toastOk(text, action = null) {
  if (!action) return toast.success(text, { duration: 4500 });
  return toast.success((t) => <Body t={t} text={text} action={action} />, { duration: 8000 });
}

/** The robot's refusal or a failure: red. */
export function toastError(text) {
  if (!text) return null;
  return toast.error(text);
}

/** A warning on the dark toast (a cancel, the partner sentence). */
export function toastWarn(text) {
  return toast(text, { icon: toastIcon('warning'), duration: 5000 });
}

/** Something started and runs in the background. */
export function toastBusy(text) {
  return toast(text, { icon: <Icon name="loading" className="animate-spin" size="1.15em" />, duration: 3000 });
}
