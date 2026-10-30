import React, { useState } from 'react';
import styles from '../DetailsPanel/DetailsPanel.module.css';

interface JsonTreeProps {
  data: unknown;
  level?: number;
}

export function JsonTree({ data, level = 0 }: JsonTreeProps) {
  const [collapsed, setCollapsed] = useState(level > 2);

  if (data === null) {
    return <span className={styles.valueNull}>null</span>;
  }

  if (data === undefined) {
    return <span className={styles.valueUndefined}>undefined</span>;
  }

  if (typeof data === 'boolean') {
    return <span className={styles.valueBool}>{data ? 'true' : 'false'}</span>;
  }

  if (typeof data === 'number') {
    return <span className={styles.valueNumber}>{data}</span>;
  }

  if (typeof data === 'string') {
    return <span className={styles.valueString}>"{data}"</span>;
  }

  if (Array.isArray(data)) {
    if (data.length === 0) {
      return <span className={styles.valueBracket}>[]</span>;
    }

    return (
      <div className={styles.tree}>
        <span
          className={styles.toggle}
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? '▶' : '▼'}
        </span>
        <span className={styles.valueBracket}>[</span>
        {collapsed ? (
          <span className={styles.collapsed} onClick={() => setCollapsed(false)}>
            {data.length} items
          </span>
        ) : (
          <>
            <div className={styles.treeChildren}>
              {data.map((item, i) => (
                <div key={i} className={styles.treeItem}>
                  <span className={styles.arrayIndex}>{i}:</span>
                  <JsonTree data={item} level={level + 1} />
                  {i < data.length - 1 && <span className={styles.comma}>,</span>}
                </div>
              ))}
            </div>
            <span className={styles.valueBracket}>]</span>
          </>
        )}
      </div>
    );
  }

  if (typeof data === 'object') {
    const entries = Object.entries(data);
    if (entries.length === 0) {
      return <span className={styles.valueBracket}>{'{}'}</span>;
    }

    return (
      <div className={styles.tree}>
        <span
          className={styles.toggle}
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? '▶' : '▼'}
        </span>
        <span className={styles.valueBracket}>{'{'}</span>
        {collapsed ? (
          <span className={styles.collapsed} onClick={() => setCollapsed(false)}>
            {entries.length} keys
          </span>
        ) : (
          <>
            <div className={styles.treeChildren}>
              {entries.map(([key, value], i) => (
                <div key={key} className={styles.treeItem}>
                  <span className={styles.key}>"{key}":</span>
                  <JsonTree data={value} level={level + 1} />
                  {i < entries.length - 1 && <span className={styles.comma}>,</span>}
                </div>
              ))}
            </div>
            <span className={styles.valueBracket}>{'}'}</span>
          </>
        )}
      </div>
    );
  }

  return <span className={styles.valueUnknown}>{String(data)}</span>;
}
