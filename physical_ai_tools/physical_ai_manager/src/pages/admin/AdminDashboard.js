import React, { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd, MdLogout, MdShield } from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import { createTeacher, listTeachers } from '../../services/adminApi';
import {
  setLoading,
  setTeachers,
  upsertTeacher,
} from '../../features/admin/adminSlice';
import CreateTeacherModal from '../../components/admin/CreateTeacherModal';
import TeacherRow from '../../components/admin/TeacherRow';

export default function AdminDashboard({ onLogout }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const fullName = useSelector((s) => s.auth.fullName);
  const username = useSelector((s) => s.auth.username);
  const teachers = useSelector((s) => s.admin.teachers);
  const loading = useSelector((s) => s.admin.loading);

  const [showCreate, setShowCreate] = useState(false);

  const fetchTeachers = useCallback(async () => {
    if (!token) return;
    dispatch(setLoading(true));
    try {
      const list = await listTeachers(token);
      dispatch(setTeachers(list));
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden');
    } finally {
      dispatch(setLoading(false));
    }
  }, [token, dispatch]);

  useEffect(() => {
    fetchTeachers();
  }, [fetchTeachers]);

  const handleCreate = async (data) => {
    const created = await createTeacher(token, data);
    dispatch(upsertTeacher(created));
  };

  const totalPool = teachers.reduce((sum, t) => sum + (t.pool_total || 0), 0);
  const totalAllocated = teachers.reduce(
    (sum, t) => sum + (t.allocated_total || 0),
    0
  );

  return (
    <div className="h-screen w-screen flex flex-col bg-gray-50">
      <header className="bg-white border-b border-gray-200 px-8 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MdShield size={26} className="text-purple-600" />
          <h1 className="text-lg font-bold text-gray-800">EduBotics - Admin-Dashboard</h1>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-sm font-medium text-gray-800">{fullName || username}</div>
            <div className="text-xs text-gray-500 font-mono">{username}</div>
          </div>
          <button
            onClick={onLogout}
            className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800 px-3 py-2 rounded-lg hover:bg-gray-100"
          >
            <MdLogout size={18} />
            Abmelden
          </button>
        </div>
      </header>

      <div className="bg-white border-b border-gray-200 px-8 py-4 flex items-center gap-6">
        <div className="flex flex-col">
          <span className="text-xs font-medium text-gray-500 uppercase">Lehrer</span>
          <span className="text-2xl font-bold text-gray-800">{teachers.length}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-xs font-medium text-gray-500 uppercase">Pool Gesamt</span>
          <span className="text-2xl font-bold text-gray-800">{totalPool}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-xs font-medium text-gray-500 uppercase">Verteilt</span>
          <span className="text-2xl font-bold text-gray-800">{totalAllocated}</span>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="ml-auto flex items-center gap-1.5 px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 text-sm font-medium"
        >
          <MdAdd size={18} />
          Neuer Lehrer
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-8">
        <div className="bg-white rounded-xl shadow-sm border border-gray-200">
          {loading ? (
            <div className="p-8 text-center text-gray-500">Laden...</div>
          ) : teachers.length === 0 ? (
            <div className="p-12 text-center text-gray-500">
              <p className="mb-4">Noch keine Lehrer.</p>
              <button
                onClick={() => setShowCreate(true)}
                className="flex items-center gap-1.5 mx-auto px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 text-sm font-medium"
              >
                <MdAdd size={18} />
                Ersten Lehrer erstellen
              </button>
            </div>
          ) : (
            <table className="w-full">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">
                    Lehrer
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Pool
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Verteilt
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Verfuegbar
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Klassen
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Schueler
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase">
                    Aktionen
                  </th>
                </tr>
              </thead>
              <tbody>
                {teachers.map((t) => (
                  <TeacherRow key={t.id} teacher={t} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {showCreate && (
        <CreateTeacherModal
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}
    </div>
  );
}
