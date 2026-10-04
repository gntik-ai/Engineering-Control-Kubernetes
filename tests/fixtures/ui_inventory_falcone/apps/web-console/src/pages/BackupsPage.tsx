import { createIamClient, listCollections, listTopics } from '@/services/dataApi'

export function BackupsPage() {
  void listCollections('wrk', 'db')
  void listTopics()
  return <button onClick={() => void createIamClient('wrk', 'cli')}>Crear cliente</button>
}
