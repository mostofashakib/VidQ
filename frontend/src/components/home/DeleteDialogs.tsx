"use client";

import { Dialog, DialogContent, DialogHeader, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface DeleteDialogsProps {
  showDialog: boolean;
  setShowDialog: (show: boolean) => void;
  onConfirmDelete: () => void;
  showDeleteAllDialog: boolean;
  setShowDeleteAllDialog: (show: boolean) => void;
  onConfirmDeleteAll: () => void;
  totalVideos: number;
}

export function DeleteDialogs({
  showDialog,
  setShowDialog,
  onConfirmDelete,
  showDeleteAllDialog,
  setShowDeleteAllDialog,
  onConfirmDeleteAll,
  totalVideos,
}: DeleteDialogsProps) {
  return (
    <>
      {/* Delete Single Video Confirmation */}
      <Dialog open={showDialog} onOpenChange={setShowDialog}>
        <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-4xl p-6 sm:p-8 shadow-2xl shadow-black">
          <DialogHeader>
            <DialogTitle className="text-xl">Delete Video</DialogTitle>
          </DialogHeader>
          <div className="text-gray-300 my-2">
            Are you sure you want to delete this video? This action cannot be undone.
          </div>
          <DialogFooter className="mt-4 gap-2">
            <Button
              variant="outline"
              onClick={() => setShowDialog(false)}
              className="rounded-xl border-white/10 hover:bg-white/10 hover:text-white text-gray-300"
            >
              Cancel
            </Button>
            <Button
              onClick={onConfirmDelete}
              className="bg-red-500/20 text-red-400 hover:bg-red-500/40 hover:text-white border border-red-500/30 transition-all rounded-xl shadow-lg shadow-red-500/20"
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete All Confirmation */}
      <Dialog open={showDeleteAllDialog} onOpenChange={setShowDeleteAllDialog}>
        <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-4xl p-6 sm:p-8 shadow-2xl shadow-black">
          <DialogHeader>
            <DialogTitle className="text-xl">Delete All Videos</DialogTitle>
          </DialogHeader>
          <div className="text-gray-300 my-2">
            This will permanently delete all {totalVideos} video{totalVideos !== 1 ? "s" : ""} in your library. This cannot be undone.
          </div>
          <DialogFooter className="mt-4 gap-2">
            <Button
              variant="outline"
              onClick={() => setShowDeleteAllDialog(false)}
              className="rounded-xl border-white/10 hover:bg-white/10 hover:text-white text-gray-300"
            >
              Cancel
            </Button>
            <Button
              onClick={onConfirmDeleteAll}
              className="bg-red-500/20 text-red-400 hover:bg-red-500/40 hover:text-white border border-red-500/30 transition-all rounded-xl shadow-lg shadow-red-500/20"
            >
              Delete All
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
