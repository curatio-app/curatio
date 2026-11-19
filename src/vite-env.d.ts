/// <reference types="vite/client" />

declare interface Window {
  api?: {
    getCredentials?: () => Promise<{ baseUrl?: string; token?: string } | null>;
  };
}

/** Injected from package.json by vite.config.ts. */
declare const __APP_VERSION__: string;
