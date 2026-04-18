import React from 'react';
import ReactDOM from 'react-dom';
import { MdClose } from 'react-icons/md';

export default function Modal({ title, onClose, children, footer, widthClass = 'max-w-md' }) {
  return ReactDOM.createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className={`bg-white rounded-[var(--radius-lg)] shadow-pop w-full ${widthClass} mx-4 max-h-[90vh] flex flex-col border border-[var(--line)]`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--line)]">
          <h3 className="text-[15px] font-semibold tracking-tight text-[var(--ink)]">
            {title}
          </h3>
          <button
            className="w-8 h-8 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] flex items-center justify-center transition"
            onClick={onClose}
            aria-label="Schließen"
          >
            <MdClose size={20} />
          </button>
        </div>
        <div className="px-6 py-5 overflow-y-auto flex-1">{children}</div>
        {footer && (
          <div className="px-6 py-4 border-t border-[var(--line)] flex justify-end gap-2">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body
  );
}
