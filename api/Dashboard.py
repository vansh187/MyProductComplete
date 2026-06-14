from fastapi import APIRouter
from utils.auth_dependency import get_current_user
from fastapi import APIRouter, Depends
#from service.dashboard.dashboardservice.DashboardService import DashboardService as dashboardService

from service.dashboard.dashboardservice.DashboardService import DashboardService 

router=APIRouter()

@router.get("/getDashboardSummary")
def getDashboardSummary(currentUser=Depends(get_current_user)):
    userId=currentUser["user_id"]
    dashboardService=DashboardService()
    userDashboard=dashboardService.getDashBoarddetailForUser(userId)
    return{
        "userId": userId,
        "DashboardSummary":userDashboard
    }

