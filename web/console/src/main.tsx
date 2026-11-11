import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { LinkProvider, ThemeProvider, TooltipProvider, gntikPreset } from '@gntik-ai/ui';
import App from './App';
import { RouterLink } from './router';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider brand={{ ...gntikPreset, name: 'Engineering Control' }}>
      <LinkProvider component={RouterLink}>
        <TooltipProvider>
          <App />
        </TooltipProvider>
      </LinkProvider>
    </ThemeProvider>
  </StrictMode>,
);
