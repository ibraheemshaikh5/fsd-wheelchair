import { useSyncExternalStore } from 'react'
import type { DrivingService } from '../services/drivingService'

export function useDriveState(service: DrivingService) {
  return useSyncExternalStore(service.subscribe, service.getState)
}
