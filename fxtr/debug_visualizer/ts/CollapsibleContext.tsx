import React, { createContext, useContext, useState } from 'react';

interface CollapsibleContextValue {
  indentLevel: number;
  forceCollapse: boolean;
}

const CollapsibleContext = createContext<CollapsibleContextValue>({
  indentLevel: 0,
  forceCollapse: false,
});

export const useCollapsibleContext = () => useContext(CollapsibleContext);

interface CollapsibleProviderProps {
  indentLevel: number;
  forceCollapse: boolean;
  children: React.ReactNode;
}

export function CollapsibleProvider({ indentLevel, forceCollapse, children }: CollapsibleProviderProps) {
  return (
    <CollapsibleContext.Provider value={{ indentLevel, forceCollapse }}>
      {children}
    </CollapsibleContext.Provider>
  );
}

interface CollapsibleContainerProps {
  incrementIndent: number;
  startCollapsed?: boolean;
  renderCollapsed: () => React.ReactNode;
  renderExpanded: (indentString: string, CollapseTarget: React.ComponentType<{ children: React.ReactNode }>) => React.ReactNode;
}

export function CollapsibleContainer({ incrementIndent, startCollapsed = false, renderCollapsed, renderExpanded }: CollapsibleContainerProps) {
  const [isCollapsed, setIsCollapsed] = useState(startCollapsed);
  const context = useCollapsibleContext();

  const handleExpandClick = (e: React.MouseEvent) => {
    // Ignore ctrl+click to allow navigation handlers to work
    if (e.ctrlKey || e.metaKey) {
      return;
    }

    if (context.forceCollapse) {
      // If force-collapsed, uncollapse locally if actually collapsed
      if (isCollapsed) {
        setIsCollapsed(false);
      }
      // Always allow event to bubble up when force-collapsed
    } else {
      // Not force-collapsed: expand locally and don't bubble
      setIsCollapsed(false);
      e.stopPropagation();
    }
  };

  const handleCollapseClick = (e: React.MouseEvent) => {
    // Ignore ctrl+click to allow navigation handlers to work
    if (e.ctrlKey || e.metaKey) {
      return;
    }

    if (context.forceCollapse) {
      // Allow event to bubble up when force-collapsed
    } else {
      // Not force-collapsed: collapse locally and don't bubble
      setIsCollapsed(true);
      e.stopPropagation();
    }
  };

  const shouldRenderCollapsed = isCollapsed || context.forceCollapse;
  const originalIndent = ' '.repeat(context.indentLevel);
  const newIndentLevel = context.indentLevel + incrementIndent;

  if (shouldRenderCollapsed) {
    // Render collapsed version with click handler to expand
    return (
      <span
        onClick={handleExpandClick}
        style={{ cursor: 'pointer' }}
      >
        <CollapsibleProvider indentLevel={newIndentLevel} forceCollapse={true}>
          {renderCollapsed()}
        </CollapsibleProvider>
      </span>
    );
  }

  // Render expanded version with incremented indent level
  const CollapseTarget: React.ComponentType<{ children: React.ReactNode }> = ({ children }) => (
    <span
      onClick={handleCollapseClick}
      style={{ cursor: 'pointer' }}
    >
      {children}
    </span>
  );

  return (
    <CollapsibleProvider indentLevel={newIndentLevel} forceCollapse={false}>
      {renderExpanded(originalIndent, CollapseTarget)}
    </CollapsibleProvider>
  );
}
