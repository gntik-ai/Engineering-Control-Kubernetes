import { LayoutGrid, Table2 } from '@gntik-ai/icons';
import { Button } from '@gntik-ai/ui';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import type { PlanRow } from './WorkspaceTabs';

export type Layout = 'table' | 'grid';

export function LayoutSwitch({ value, change }: { value: Layout; change: (mode: Layout) => void }) {
  return (
    <div
      role="group"
      aria-label="Display mode"
      className="inline-flex gap-0.5 rounded-md border border-border bg-card p-0.5"
    >
      {(['table', 'grid'] as const).map((mode) => (
        <Button
          key={mode}
          size="sm"
          type="button"
          variant={mode === value ? 'secondary' : 'ghost'}
          icon={mode === 'table' ? Table2 : LayoutGrid}
          aria-pressed={mode === value}
          onClick={() => change(mode)}
        >
          {mode === 'table' ? 'Table' : 'Grid list'}
        </Button>
      ))}
    </div>
  );
}

// The gntik-ui Resource Card Grid uses container-width breakpoints. Each card uses the
// table's record renderers so that its links, counts and approval actions stay identical.
export function PlanningGrid({
  rows,
  columns,
  label,
}: {
  rows: readonly PlanRow[];
  columns: DataTableColumn<PlanRow>[];
  label: string;
}) {
  const [title, ...others] = columns;
  const metadata = others.filter((c) => c.id !== 'actions');
  const actions = others.find((c) => c.id === 'actions');
  return (
    <div className="@container w-full min-w-0">
      <ul
        aria-label={label + ' grid list'}
        className="grid grid-cols-1 gap-4 @xl:grid-cols-2 @4xl:grid-cols-3"
      >
        {rows.map((row) => (
          <li
            key={row.id}
            aria-label={row.title}
            className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-sm [&:has([aria-current=location])]:ring-2"
          >
            <div className="break-words">{title?.cell?.(row) ?? row.title}</div>
            <dl className="grid gap-2 text-sm">
              {metadata.map((column) => (
                <div key={column.id}>
                  <dt className="text-xs text-muted-foreground">{column.header}</dt>
                  <dd>{column.cell ? column.cell(row) : String(column.accessor?.(row) ?? '—')}</dd>
                </div>
              ))}
            </dl>
            {actions && (
              <div className="mt-auto border-t border-border pt-3">{actions.cell?.(row)}</div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
