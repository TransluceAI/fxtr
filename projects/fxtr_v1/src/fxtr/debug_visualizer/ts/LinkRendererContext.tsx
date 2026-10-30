import React, { createContext, useContext, ReactNode } from 'react';
import { CidString } from './EscapedJsonTypes';

// Type for the link renderer function
export type LinkRendererFunction = (cid: CidString, spec: any | null, ipldPath?: any[]) => ReactNode;

// Default implementation that renders CID as orange-outlined link to /view/entity/{cid}
export const defaultLinkRenderer: LinkRendererFunction = (cid: CidString, spec: any | null, ipldPath?: any[]) => {
  return (
    <a
      href={`/view/entity/${cid.base32_cid_str}`}
      style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
        color: '#ff8c00', // Orange color for CIDs
        border: '1px solid #ff8c00',
        borderRadius: '3px',
        padding: '1px 4px',
        fontSize: '0.9em',
        textDecoration: 'none'
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.backgroundColor = 'rgba(255, 140, 0, 0.1)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.backgroundColor = 'transparent';
      }}
    >
      CID({cid.base32_cid_str})
    </a>
  );
};

// Create the context
const LinkRendererContext = createContext<LinkRendererFunction>(defaultLinkRenderer);

// Hook to use the link renderer
export const useLinkRenderer = () => useContext(LinkRendererContext);

// Provider component
interface LinkRendererProviderProps {
  linkRenderer?: LinkRendererFunction;
  children: ReactNode;
}

export function LinkRendererProvider({ linkRenderer, children }: LinkRendererProviderProps) {
  return (
    <LinkRendererContext.Provider value={linkRenderer || defaultLinkRenderer}>
      {children}
    </LinkRendererContext.Provider>
  );
}
