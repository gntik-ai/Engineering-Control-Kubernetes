import { StrictMode, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { LinkProvider, ThemeProvider, TooltipProvider, gntikPreset } from '@gntik-ai/ui';
import { RouterLink } from './router';
import './index.css';

/** Mounts a page inside the providers every page of the console shares. */
export function mount(page: ReactNode) {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ThemeProvider brand={{ ...gntikPreset, name: 'Engineering Control' }}>
        <LinkProvider component={RouterLink}>
          <TooltipProvider>{page}</TooltipProvider>
        </LinkProvider>
      </ThemeProvider>
    </StrictMode>,
  );
}
