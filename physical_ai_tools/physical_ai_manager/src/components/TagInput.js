// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: Kiwoong Park

import React, { useState, useRef } from 'react';
import clsx from 'clsx';
import Icon from './icons/Icon';

const TagInput = ({ tags, onChange, disabled, className }) => {
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef(null);

  const addTag = (tag) => {
    const trimmedTag = tag.trim();
    if (trimmedTag && !tags.includes(trimmedTag)) {
      onChange([...tags, trimmedTag]);
    }
    setInputValue('');
  };

  const removeTag = (indexToRemove) => {
    onChange(tags.filter((_, index) => index !== indexToRemove));
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTag(inputValue);
    } else if (e.key === 'Backspace' && inputValue === '' && tags.length > 0) {
      // Remove last tag when backspace is pressed and input is empty
      removeTag(tags.length - 1);
    }
  };

  const handleInputChange = (e) => {
    const value = e.target.value;
    // Allow comma-separated input
    if (value.includes(',')) {
      const newTags = value
        .split(',')
        .map((t) => t.trim())
        .filter((t) => t && !tags.includes(t));
      if (newTags.length > 0) {
        onChange([...tags, ...newTags]);
      }
      setInputValue('');
    } else {
      setInputValue(value);
    }
  };

  return (
    <div
      className={clsx(
        // Sucher (§8): a well field (su-field) with key-look chips.
        'su-field',
        'flex',
        'flex-wrap',
        'gap-1',
        'p-2',
        'min-h-10',
        'max-w-full',
        'w-full',
        'text-sm',
        'focus-within:ring-2',
        'focus-within:ring-[#FF5B1F]',
        'max-h-24',
        'overflow-y-auto',
        {
          'cursor-not-allowed': disabled,
        },
        className
      )}
      onClick={() => {
        if (!disabled && inputRef.current) {
          inputRef.current.focus();
        }
      }}
    >
      {/* Render existing tags */}
      {tags.map((tag, index) => (
        <span
          key={index}
          className={clsx(
            'inline-flex',
            'items-center',
            'px-2',
            'py-1',
            'bg-[#FFFFFF]',
            'text-gray-800',
            'shadow-[0_0_0_1px_#CFCAC0,0_2px_0_#C9C4BA]',
            'text-xs',
            'font-medium',
            'rounded-full',
            'max-w-[80%]',
            'break-all',
            {
              'cursor-default': disabled,
              'cursor-pointer': !disabled,
            }
          )}
        >
          {tag}
          {!disabled && (
            <button
              type="button"
              onClick={() => removeTag(index)}
              className={clsx('ml-1', 'text-gray-500', 'hover:text-gray-800')}
              aria-label={`${tag} entfernen`}
              title={`${tag} entfernen`}
            >
              <Icon name="close" />
            </button>
          )}
        </span>
      ))}

      {/* Input for new tags */}
      <input
        ref={inputRef}
        type="text"
        value={inputValue}
        onChange={handleInputChange}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        placeholder={tags.length === 0 ? 'Tags hinzufügen' : ''}
        className={clsx(
          'w-auto',
          'min-w-12',
          'max-w-[40%]',
          'break-all',
          'bg-transparent',
          'border-none',
          'outline-none',
          'text-sm',
          {
            'cursor-not-allowed': disabled,
            'cursor-text': !disabled,
          }
        )}
      />
    </div>
  );
};

export default TagInput;
