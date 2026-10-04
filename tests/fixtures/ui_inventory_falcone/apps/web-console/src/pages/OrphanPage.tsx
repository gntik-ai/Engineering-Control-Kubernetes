import { listDenials } from '@/services/legacyApi'

export function OrphanPage() {
  void listDenials()
  return <p>orphan</p>
}
