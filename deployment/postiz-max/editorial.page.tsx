import { Metadata } from 'next';
import { EditorialPanel } from '@gitroom/frontend/components/editorial/editorial-panel';

export const dynamic = 'force-dynamic';
export const metadata: Metadata = { title: 'Postiz — Редакция' };

export default function EditorialPage() { return <EditorialPanel />; }
