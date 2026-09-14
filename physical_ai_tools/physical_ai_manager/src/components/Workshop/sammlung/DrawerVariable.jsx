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
 * Sammlung drawer detail of ONE Blockly variable. Renaming refuses a name
 * another variable already holds (Blockly would silently merge the two);
 * deleting goes through Blockly's own „Variable löschen", confirmation and all.
 */

import React from 'react';
import { useDispatch } from 'react-redux';
import toast from 'react-hot-toast';
import { setDrawerFocus } from '../../../features/workshop/studioAssetsSlice';
import { DE } from '../blocks/messages_de';
import { deleteVariable, renameVariable, usageRows } from './assetCommands';
import { RenameField, UsageList } from './drawerParts';

export default function DrawerVariable({ workspace, card }) {
  const dispatch = useDispatch();
  const rows = usageRows(workspace, 'variable', card.assetId);

  const handleRename = (draft) => {
    const result = renameVariable({ workspace, variableId: card.assetId, toName: draft });
    if (!result.ok) {
      toast.error(result.error);
      return false;
    }
    return true;
  };

  const handleDelete = () => {
    const result = deleteVariable({ workspace, variableId: card.assetId });
    if (!result.ok) {
      toast.error(result.error);
      return;
    }
    // Blockly may still be asking; if the student declines, the variable stays
    // in the list and can be picked again.
    dispatch(setDrawerFocus(null));
  };

  return (
    <div className="p-3">
      <RenameField key={card.assetName} name={card.assetName} maxLength={64} onCommit={handleRename} />
      <UsageList workspace={workspace} rows={rows} nowhereText={DE.DRAWER_USED_NOWHERE_VARIABLE} />
      <button
        type="button"
        onClick={handleDelete}
        className="mt-4 rounded border border-red-300 px-2 py-1 text-sm text-red-700 hover:bg-red-50"
      >
        {DE.DRAWER_DELETE_VARIABLE}
      </button>
    </div>
  );
}
