from database.dashboardpersistence.DashboardPersistence import DashBoardPersistence 
from utils.redisConnection import RedisConnection
class DashboardService:

   
    
    
    def getDashBoarddetailForUser(self,userId):
            dashBoardPersistence=DashBoardPersistence()
            userDashBoard= dashBoardPersistence.getDashboardDetails(userId)
            ##redis logic cache will implement
            return userDashBoard
    