import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation } from '@tanstack/react-query';
import { Button, Input } from '@/components/ui';
import { petSpaceApi, type PetProjectFileRow } from '@/api/petSpace';
import { fetchAuthedFilePreviewBlob } from '@/hooks/useAuthedFileBlobUrl';
import { isPetRenderableImageRow } from '@/lib/petAppearanceMime';
import { loadImageElement, spriteSheetToGifBlob, type SpriteSheetGifMeta } from '@/lib/spriteSheetToGif';

export function SpriteSheetGifTool({
  projectId,
  files,
  onDone,
}: {
  projectId: string;
  files: PetProjectFileRow[];
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const images = useMemo(() => files.filter((f) => isPetRenderableImageRow(f)), [files]);
  const [sourceId, setSourceId] = useState<string | null>(() => images[0]?.file_id ?? null);
  const [cols, setCols] = useState(4);
  const [rows, setRows] = useState(4);
  const [pad, setPad] = useState(0);
  const [fps, setFps] = useState(8);
  const [transparent, setTransparent] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewMeta, setPreviewMeta] = useState<SpriteSheetGifMeta | null>(null);
  const [sourceDims, setSourceDims] = useState<{ w: number; h: number } | null>(null);

  const srcRow = images.find((f) => f.file_id === sourceId) ?? null;

  useEffect(() => {
    if (!images.find((f) => f.file_id === sourceId) && images[0]) {
      setSourceId(images[0]!.file_id);
    }
  }, [images, sourceId]);

  useEffect(() => {
    const row = images.find((f) => f.file_id === sourceId) ?? null;
    if (!row) {
      setSourceDims(null);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    void (async () => {
      try {
        const blob = await fetchAuthedFilePreviewBlob(row.file_id, row.mime_type, row.original_name);
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        const img = await loadImageElement(objectUrl);
        if (!cancelled) setSourceDims({ w: img.naturalWidth, h: img.naturalHeight });
      } catch {
        if (!cancelled) setSourceDims(null);
      } finally {
        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [images, sourceId]);

  const uploadMut = useMutation({
    mutationFn: ({ pid, file }: { pid: string; file: File }) => petSpaceApi.uploadFile(pid, file),
    onSuccess: () => {
      onDone();
    },
  });

  const generate = async () => {
    setErr(null);
    const row = images.find((f) => f.file_id === sourceId) ?? null;
    if (!row) {
      setErr(t('petSpace.spriteNeedImage'));
      return;
    }
    let objectUrl: string | null = null;
    setBusy(true);
    try {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
        setPreviewUrl(null);
      }
      setPreviewMeta(null);
      const sourcePreviewBlob = await fetchAuthedFilePreviewBlob(
        row.file_id,
        row.mime_type,
        row.original_name,
      );
      objectUrl = URL.createObjectURL(sourcePreviewBlob);
      const img = await loadImageElement(objectUrl);
      const result = await spriteSheetToGifBlob(img, {
        cols,
        rows,
        pad,
        fps,
        transparent,
        loop: 0,
      });
      const { blob: gifBlob, ...meta } = result;
      setPreviewMeta(meta);
      setPreviewUrl(URL.createObjectURL(gifBlob));
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setBusy(false);
    }
  };

  const uploadGenerated = async () => {
    if (!previewUrl) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(previewUrl);
      const blob = await res.blob();
      const file = new File([blob], 'pet-sprite.gif', { type: 'image/gif' });
      uploadMut.mutate({ pid: projectId, file });
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  const spritePreviewScale = useMemo(
    () =>
      previewMeta
        ? Math.min(4, Math.max(1, Math.floor(288 / Math.max(previewMeta.frameWidth, previewMeta.frameHeight))))
        : 1,
    [previewMeta],
  );

  if (images.length === 0) {
    return (
      <p className="text-sm text-muted-foreground border border-dashed border-border rounded-xl p-4">
        {t('petSpace.spriteNoImages')}
      </p>
    );
  }

  return (
    <div className="rounded-xl border border-border p-4 space-y-4 bg-surface-sunken/20">
      <h3 className="text-sm font-semibold">{t('petSpace.spriteTitle')}</h3>
      <p className="text-xs text-muted-foreground">{t('petSpace.spriteIntro')}</p>
      <p className="text-xs text-muted-foreground-tertiary">{t('petSpace.spriteFormatsNote')}</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="text-xs space-y-1 block">
          <span className="text-muted-foreground">{t('petSpace.spriteSource')}</span>
          <select
            className="w-full rounded-lg border border-border bg-background px-2 py-1.5 text-sm"
            value={sourceId ?? images[0]?.file_id ?? ''}
            onChange={(e) => setSourceId(e.target.value || null)}
          >
            {images.map((f) => (
              <option key={f.id} value={f.file_id}>
                {f.original_name}
              </option>
            ))}
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          <label className="text-xs space-y-1 block">
            <span className="text-muted-foreground">{t('petSpace.spriteCols')}</span>
            <Input type="number" min={1} max={32} value={cols} onChange={(e) => setCols(+e.target.value || 1)} />
          </label>
          <label className="text-xs space-y-1 block">
            <span className="text-muted-foreground">{t('petSpace.spriteRows')}</span>
            <Input type="number" min={1} max={32} value={rows} onChange={(e) => setRows(+e.target.value || 1)} />
          </label>
          <label className="text-xs space-y-1 block">
            <span className="text-muted-foreground">{t('petSpace.spritePad')}</span>
            <Input type="number" min={0} max={64} value={pad} onChange={(e) => setPad(+e.target.value || 0)} />
          </label>
          <label className="text-xs space-y-1 block">
            <span className="text-muted-foreground">{t('petSpace.spriteFps')}</span>
            <Input type="number" min={1} max={60} value={fps} onChange={(e) => setFps(+e.target.value || 8)} />
          </label>
        </div>
      </div>
      {sourceDims ? (
        <div className="space-y-1 rounded-lg border border-border-subtle bg-background/50 px-2 py-2 text-xs text-muted-foreground">
          <p>
            {t('petSpace.spriteSourceDims', { w: sourceDims.w, h: sourceDims.h })}
            {' · '}
            {t('petSpace.spriteCellEstimate', {
              cols,
              rows,
              cw: Math.floor(sourceDims.w / cols),
              ch: Math.floor(sourceDims.h / rows),
            })}
            {' · '}
            {t('petSpace.spriteInnerEstimate', {
              iw: Math.max(0, Math.floor(sourceDims.w / cols) - pad * 2),
              ih: Math.max(0, Math.floor(sourceDims.h / rows) - pad * 2),
            })}
          </p>
          {sourceDims.w % cols !== 0 || sourceDims.h % rows !== 0 ? (
            <p className="text-amber-700 dark:text-amber-300">
              {t('petSpace.spriteGridRemainder', {
                rx: sourceDims.w % cols,
                ry: sourceDims.h % rows,
              })}
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="space-y-1">
        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input type="checkbox" checked={transparent} onChange={(e) => setTransparent(e.target.checked)} />
          {t('petSpace.spriteTransparent')}
        </label>
        <p className="text-[11px] text-muted-foreground leading-snug pl-5">{t('petSpace.spriteAlphaNote')}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" disabled={busy || !srcRow} onClick={() => void generate()}>
          {t('petSpace.spriteGenerate')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy || !previewUrl || uploadMut.isPending}
          onClick={() => void uploadGenerated()}
        >
          {t('petSpace.spriteUpload')}
        </Button>
      </div>
      {err ? <p className="text-xs text-red-600">{err}</p> : null}
      {previewUrl && previewMeta ? (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            {t('petSpace.spritePreviewOut', {
              fw: previewMeta.frameWidth,
              fh: previewMeta.frameHeight,
              n: previewMeta.frameCount,
            })}
          </p>
          <div className="inline-block rounded-lg border border-border-subtle bg-muted/30 p-1 dark:bg-muted/20">
            <img
              key={previewUrl}
              src={previewUrl}
              alt=""
              className="block rounded-md border border-border-subtle bg-transparent"
              style={{
                width: previewMeta.frameWidth * spritePreviewScale,
                height: previewMeta.frameHeight * spritePreviewScale,
                imageRendering: 'pixelated',
              }}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
