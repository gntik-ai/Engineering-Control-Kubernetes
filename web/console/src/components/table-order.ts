import { useState } from 'react';
import type { SortDirection } from '@gntik-ai/ui';
import {
  sortRows,
  type CellValue,
  type SortState,
} from './blocks/tables/data-table/data-table-utils';

export function sortParams(sort: SortState | null) {
  return sort
    ? { sort: sort.columnId, direction: sort.direction === 'descending' ? 'desc' : 'asc' }
    : {};
}

export function querySort(query: URLSearchParams, columns: readonly string[]): SortState | null {
  const columnId = query.get('sort');
  return columnId && columns.includes(columnId)
    ? { columnId, direction: query.get('direction') === 'desc' ? 'descending' : 'ascending' }
    : null;
}

export function useTableOrder(resetPage?: () => void) {
  const [sort, setSort] = useState<SortState | null>(null);
  function change(next: SortState) {
    setSort(next);
    resetPage?.();
  }
  return {
    sort,
    change,
    header(columnId: string) {
      return {
        sortable: true,
        sortDirection: (sort?.columnId === columnId ? sort.direction : 'none') as SortDirection,
        onSortChange: (direction: 'ascending' | 'descending') => change({ columnId, direction }),
      };
    },
    rows<T>(items: readonly T[], values: Record<string, (row: T) => CellValue>): readonly T[] {
      if (!sort) return items;
      const accessor = values[sort.columnId];
      return accessor
        ? sortRows(items, [{ id: sort.columnId, header: sort.columnId, accessor }], sort)
        : items;
    },
  };
}
