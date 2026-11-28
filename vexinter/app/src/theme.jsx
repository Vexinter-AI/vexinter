import { createContext, useContext, useEffect, useState } from 'react';

const C = createContext(null);
export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(
    () => localStorage.getItem('vex_theme') || 'dark');
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('vex_theme', theme);
  }, [theme]);
  return <C.Provider value={{ theme, setTheme, isDark: theme !== 'light' }}>
    {children}</C.Provider>;
}
export const useTheme = () => useContext(C);