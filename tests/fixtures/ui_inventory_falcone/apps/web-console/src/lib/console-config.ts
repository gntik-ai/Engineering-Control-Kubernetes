function readEnv(name: string, fallback: string): string {
  return (import.meta.env[name] as string | undefined) ?? fallback
}

export const consoleAuthConfig = {
  loginPath: readEnv('VITE_CONSOLE_AUTH_LOGIN_PATH', '/login')
}
