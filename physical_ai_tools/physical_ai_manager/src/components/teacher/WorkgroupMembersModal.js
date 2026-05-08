import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd, MdDelete } from 'react-icons/md';
import Modal from './Modal';
import { Avatar, Btn, Pill } from '../EbUI';
import {
  addWorkgroupMember,
  removeWorkgroupMember,
} from '../../services/workgroupsApi';

const MAX_GROUP_SIZE = 10;

/**
 * Add / remove members for a workgroup. Eligibility filter:
 *   - student must be in the same classroom as the group
 *   - student must not already be in another group
 *
 * Receives the parent's `onChanged` callback to bubble up the refresh
 * (group detail + classroom detail both want the update).
 */
export default function WorkgroupMembersModal({
  token,
  classroomStudents,
  workgroup,
  onClose,
  onChanged,
}) {
  const [busy, setBusy] = useState(false);
  const [draft, setWorkgroup] = useState(workgroup);
  const members = draft.members || [];
  const memberIds = new Set(members.map((m) => m.id));

  const eligible = (classroomStudents || []).filter(
    (s) =>
      !memberIds.has(s.id) &&
      (!s.workgroup_id || s.workgroup_id === draft.id)
  );

  const handleAdd = async (studentId) => {
    if (busy || members.length >= MAX_GROUP_SIZE) return;
    setBusy(true);
    try {
      const updated = await addWorkgroupMember(token, draft.id, studentId);
      setWorkgroup(updated);
      onChanged?.(updated);
      toast.success('Mitglied hinzugefügt');
    } catch (err) {
      toast.error(err.message || 'Fehler beim Hinzufügen');
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (studentId) => {
    if (busy) return;
    if (!window.confirm('Mitglied aus der Gruppe entfernen?')) return;
    setBusy(true);
    try {
      await removeWorkgroupMember(token, draft.id, studentId);
      const next = {
        ...draft,
        member_count: Math.max((draft.member_count || 1) - 1, 0),
        members: members.filter((m) => m.id !== studentId),
      };
      setWorkgroup(next);
      onChanged?.(next);
      toast.success('Mitglied entfernt');
    } catch (err) {
      toast.error(err.message || 'Fehler beim Entfernen');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Mitglieder · ${draft.name}`}
      widthClass="max-w-lg"
      onClose={onClose}
      footer={
        <Btn variant="primary" onClick={onClose}>
          Fertig
        </Btn>
      }
    >
      <div className="flex flex-col gap-5">
        <section>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)] mb-2">
            Aktuelle Mitglieder · {members.length} / {MAX_GROUP_SIZE}
          </div>
          {members.length === 0 ? (
            <p className="text-sm text-[var(--ink-3)] py-2">
              Noch keine Mitglieder. Füge unten Schüler hinzu.
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {members.map((m) => (
                <li
                  key={m.id}
                  className="flex items-center gap-3 px-3 py-2 bg-[var(--bg-sunk)] rounded-[var(--radius-sm)]"
                >
                  <Avatar name={m.full_name || m.username} size={28} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-[var(--ink)] truncate">
                      {m.full_name || m.username}
                    </div>
                    <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
                      {m.username}
                    </div>
                  </div>
                  <Btn
                    variant="ghost"
                    size="sm"
                    onClick={() => handleRemove(m.id)}
                    disabled={busy}
                    title="Entfernen"
                  >
                    <MdDelete size={16} />
                  </Btn>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)] mb-2">
            Hinzufügen
          </div>
          {eligible.length === 0 ? (
            <p className="text-sm text-[var(--ink-3)] py-2">
              Keine verfügbaren Schüler. Schüler einer anderen Gruppe müssen zuerst entfernt werden.
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {eligible.map((s) => (
                <li
                  key={s.id}
                  className="flex items-center gap-3 px-3 py-2 bg-white border border-[var(--line)] rounded-[var(--radius-sm)]"
                >
                  <Avatar name={s.full_name || s.username} size={28} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-[var(--ink)] truncate flex items-center gap-2">
                      {s.full_name || s.username}
                      {s.workgroup_name && (
                        <Pill tone="amber">In Gruppe {s.workgroup_name}</Pill>
                      )}
                    </div>
                    <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
                      {s.username}
                    </div>
                  </div>
                  <Btn
                    variant="primary"
                    size="sm"
                    onClick={() => handleAdd(s.id)}
                    disabled={busy || members.length >= MAX_GROUP_SIZE}
                    title="Hinzufügen"
                  >
                    <MdAdd size={16} />
                  </Btn>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Modal>
  );
}
