import { requestConsoleSessionJson } from '@/lib/console-session'

export function listCollections(workspaceId: string, databaseName: string) {
  return requestConsoleSessionJson<string[]>(
    `/v1/mongo/workspaces/${workspaceId}/data/${databaseName}/collections`
  )
}

export function listTopics() {
  return requestConsoleSessionJson<string[]>('/v1/events/topics')
}

export function createIamClient(workspaceId: string, clientId: string) {
  return requestConsoleSessionJson<void>(`/v1/workspaces/${workspaceId}/iam/clients`, {
    method: 'POST',
    body: { clientId }
  })
}
