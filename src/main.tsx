import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/global.css';

// Navigation is a history-backed overlay stack (app/navStack.ts); the dashboard
// has no routes of its own, so no router wraps it.
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
