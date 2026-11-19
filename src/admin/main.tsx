import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import AdminApp from './AdminApp';
import '../styles/tokens.css';
import './styles.css';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: true } },
});

ReactDOM.createRoot(document.getElementById('admin-root')!).render(
  <React.StrictMode><QueryClientProvider client={queryClient}><BrowserRouter basename="/admin"><AdminApp /></BrowserRouter></QueryClientProvider></React.StrictMode>,
);
